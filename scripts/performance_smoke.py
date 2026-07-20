"""Concurrent, disposable performance smoke for a deployed VMA Organization.

The smoke never modifies an existing Agent, Environment, or Vault.  It creates
ten independent Sessions by default, sends one turn to all of them at once,
measures durable server event timestamps, and cleans up every resource it owns
(deleting Sessions/Environment and archiving its Agent).

Credentials are accepted through environment variables only so they do not
appear in shell history or the process list.  Run ``--help`` for the complete
configuration contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urlsplit


# Keep this script runnable from a source checkout without publishing or
# installing the native SDK first.
SDK_SOURCE = Path(__file__).resolve().parents[1] / "sdks" / "python" / "src"
if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from votrix import AsyncVotrix  # noqa: E402


DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_SESSION_COUNT = 10
TERMINAL_EVENTS = frozenset({"session.status_idle", "session.status_terminated"})
STATUS_EVENTS = frozenset(
    {
        "session.status_idle",
        "session.status_rescheduled",
        "session.status_running",
        "session.status_terminated",
    }
)


@dataclass(frozen=True)
class SmokeConfig:
    vault_ids: tuple[str, ...]
    session_count: int = DEFAULT_SESSION_COUNT
    agent_id: str | None = None
    environment_id: str | None = None
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    provision_concurrency: int = 2
    turn_timeout: float = 900.0
    # Ten Sessions at two-second polling is ~300 list requests/minute, leaving
    # headroom under the managed 600 RPM Organization limit. Latencies use server
    # timestamps, so this cadence does not inflate the reported measurements.
    poll_interval: float = 2.0
    cleanup_timeout: float = 90.0
    max_queue_wait: float | None = None
    max_first_event: float | None = None
    max_total_latency: float | None = None


@dataclass
class TurnResult:
    index: int
    session_id: str | None = None
    success: bool = False
    failure_stage: str | None = None
    error: str | None = None
    provision_ms: float | None = None
    trigger_http_ms: float | None = None
    queue_wait_ms: float | None = None
    first_event_ms: float | None = None
    first_event_type: str | None = None
    total_ms: float | None = None
    terminal_event_type: str | None = None
    stop_reason_type: str | None = None


@dataclass
class OwnedResources:
    session_ids: list[str] = field(default_factory=list)
    agent_id: str | None = None
    environment_id: str | None = None


@dataclass
class CleanupReport:
    deleted_sessions: list[str] = field(default_factory=list)
    archived_agent: bool = False
    deleted_environment: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class SmokeReport:
    run_id: str
    requested_sessions: int
    created_sessions: int
    results: list[TurnResult]
    cleanup: CleanupReport
    setup_error: str | None = None

    @property
    def passed(self) -> int:
        return sum(result.success for result in self.results)

    @property
    def failed(self) -> int:
        return self.requested_sessions - self.passed

    @property
    def ok(self) -> bool:
        return (
            self.setup_error is None
            and self.created_sessions == self.requested_sessions
            and self.passed == self.requested_sessions
            and not self.cleanup.errors
        )

    def to_dict(self, *, target: str) -> dict[str, Any]:
        return {
            "type": "vma_performance_smoke",
            "run_id": self.run_id,
            "target": target,
            "ok": self.ok,
            "requested_sessions": self.requested_sessions,
            "created_sessions": self.created_sessions,
            "passed": self.passed,
            "failed": self.failed,
            "setup_error": self.setup_error,
            "latency_ms": {
                "provision": _metric_summary(self.results, "provision_ms"),
                "trigger_http": _metric_summary(self.results, "trigger_http_ms"),
                "queue_wait": _metric_summary(self.results, "queue_wait_ms"),
                "first_event": _metric_summary(self.results, "first_event_ms"),
                "total": _metric_summary(self.results, "total_ms"),
            },
            "results": [asdict(result) for result in self.results],
            "cleanup": asdict(self.cleanup),
        }


@dataclass
class _TurnObservation:
    accepted_at: datetime | None
    trigger_started: float
    queue_wait_ms: float | None = None
    first_event_ms: float | None = None
    first_event_type: str | None = None
    total_ms: float | None = None
    terminal_event_type: str | None = None
    stop_reason_type: str | None = None
    response_contains_nonce: bool = False

    def observe(self, event: Any, *, nonce: str, now: float) -> None:
        event_type = str(_value(event, "type") or "")
        fallback_ms = max(0.0, (now - self.trigger_started) * 1000.0)
        event_ms = _event_latency_ms(event, self.accepted_at, fallback_ms)

        if event_type == "session.status_running" and self.queue_wait_ms is None:
            self.queue_wait_ms = event_ms
        if self.first_event_ms is None and _is_runtime_event(event_type):
            self.first_event_ms = event_ms
            self.first_event_type = event_type
        if event_type == "agent.message" and nonce in _serialized_event(event):
            self.response_contains_nonce = True
        if event_type in TERMINAL_EVENTS:
            self.total_ms = event_ms
            self.terminal_event_type = event_type
            reason = _value(event, "stop_reason")
            if isinstance(reason, dict):
                self.stop_reason_type = str(reason.get("type") or "") or None
            elif reason is not None:
                self.stop_reason_type = str(_value(reason, "type") or "") or None


async def run_smoke(
    client: Any,
    config: SmokeConfig,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SmokeReport:
    """Run the smoke with an AsyncVotrix-compatible client.

    ``client`` is injected to keep the orchestration testable without network
    access.  The returned report contains identifiers and timings, never
    request bodies or credential values.
    """

    _validate_config(config)
    run_id = f"vma-perf-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    owned = OwnedResources()
    results = [TurnResult(index=index + 1) for index in range(config.session_count)]
    cleanup = CleanupReport()
    setup_error: str | None = None

    try:
        for vault_id in config.vault_ids:
            await client.vaults.retrieve(vault_id)

        agent_id = config.agent_id
        if agent_id is None:
            agent = await client.agents.create(
                name=f"VMA performance smoke {run_id}",
                model={"id": config.model, "provider": config.provider},
                system=(
                    "You are serving an automated VMA performance smoke. "
                    "Follow the response-format instruction exactly and do not use tools."
                ),
                metadata={"created_by": "scripts/performance_smoke.py", "smoke_run_id": run_id},
            )
            agent_id = str(agent.id)
            owned.agent_id = agent_id
        else:
            await client.agents.retrieve(agent_id)

        environment_id = config.environment_id
        if environment_id is None:
            environment = await client.environments.create(
                name=f"VMA performance smoke {run_id}",
                config={"type": "cloud"},
                metadata={"created_by": "scripts/performance_smoke.py", "smoke_run_id": run_id},
            )
            environment_id = str(environment.id)
            owned.environment_id = environment_id
        else:
            await client.environments.retrieve(environment_id)

        provision_semaphore = asyncio.Semaphore(config.provision_concurrency)

        async def provision(result: TurnResult) -> None:
            async with provision_semaphore:
                started = time.monotonic()
                try:
                    session = await client.sessions.create(
                        agent=agent_id,
                        environment_id=environment_id,
                        title=f"Performance smoke {run_id} #{result.index}",
                        metadata={
                            "created_by": "scripts/performance_smoke.py",
                            "smoke_run_id": run_id,
                            "smoke_index": str(result.index),
                        },
                        vault_ids=list(config.vault_ids),
                        idempotency_key=f"{run_id}-session-{result.index}",
                    )
                except Exception as exc:
                    result.failure_stage = "provision"
                    result.error = _safe_error(exc)
                    return
                result.provision_ms = (time.monotonic() - started) * 1000.0
                result.session_id = str(session.id)
                owned.session_ids.append(result.session_id)

        await asyncio.gather(*(provision(result) for result in results))

        provisioned = [result for result in results if result.session_id is not None]
        if not provisioned:
            setup_error = "No disposable Sessions could be provisioned"
        else:
            start_gate = asyncio.Event()

            async def exercise(result: TurnResult) -> None:
                assert result.session_id is not None
                nonce = f"{run_id}-turn-{result.index}"
                await start_gate.wait()
                trigger_started = time.monotonic()
                observation: _TurnObservation | None = None
                try:
                    sent = await client.sessions.events.send(
                        result.session_id,
                        events=[
                            {
                                "type": "user.message",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "Reply with exactly this nonce and nothing else: "
                                            f"{nonce}"
                                        ),
                                    }
                                ],
                            }
                        ],
                        idempotency_key=f"{run_id}-event-{result.index}",
                    )
                    result.trigger_http_ms = (time.monotonic() - trigger_started) * 1000.0
                    accepted = sent.data[-1]
                    after_seq = int(_value(accepted, "seq") or 0)
                    accepted_at = _as_datetime(_value(accepted, "created_at"))
                    observation = _TurnObservation(
                        accepted_at=accepted_at,
                        trigger_started=trigger_started,
                    )
                    await _monitor_turn(
                        client,
                        result.session_id,
                        after_seq=after_seq,
                        nonce=nonce,
                        observation=observation,
                        timeout=config.turn_timeout,
                        poll_interval=config.poll_interval,
                        sleep=sleep,
                    )
                    _finish_result(result, observation, config)
                except Exception as exc:
                    if observation is not None:
                        _copy_observation(result, observation)
                    result.failure_stage = result.failure_stage or "turn"
                    result.error = _safe_error(exc)

            tasks = [asyncio.create_task(exercise(result)) for result in provisioned]
            # Every task is waiting before the gate opens, making the event
            # submissions genuinely concurrent without synchronizing setup I/O.
            await sleep(0)
            start_gate.set()
            await asyncio.gather(*tasks)
    except Exception as exc:
        setup_error = _safe_error(exc)
    finally:
        cleanup = await cleanup_owned_resources(
            client,
            owned,
            timeout=config.cleanup_timeout,
            poll_interval=min(1.0, max(0.05, config.poll_interval)),
            concurrency=config.provision_concurrency,
            sleep=sleep,
        )

    return SmokeReport(
        run_id=run_id,
        requested_sessions=config.session_count,
        created_sessions=len(owned.session_ids),
        results=results,
        cleanup=cleanup,
        setup_error=setup_error,
    )


async def _monitor_turn(
    client: Any,
    session_id: str,
    *,
    after_seq: int,
    nonce: str,
    observation: _TurnObservation,
    timeout: float,
    poll_interval: float,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    deadline = time.monotonic() + timeout
    cursor = after_seq
    while time.monotonic() < deadline:
        events = [
            event
            async for event in client.sessions.events.list(
                session_id,
                after_seq=cursor,
                limit=100,
                order="asc",
            )
        ]
        for event in events:
            seq = int(_value(event, "seq") or 0)
            cursor = max(cursor, seq)
            observation.observe(event, nonce=nonce, now=time.monotonic())
            if observation.terminal_event_type is not None:
                return
        await sleep(poll_interval)
    raise TimeoutError(f"Session {session_id} did not reach a terminal event within {timeout:.1f}s")


def _finish_result(
    result: TurnResult,
    observation: _TurnObservation,
    config: SmokeConfig,
) -> None:
    _copy_observation(result, observation)

    if observation.terminal_event_type == "session.status_terminated":
        result.failure_stage = "runtime"
        result.error = "Session terminated"
        return
    if observation.terminal_event_type != "session.status_idle":
        result.failure_stage = "runtime"
        result.error = "Session did not emit a terminal event"
        return
    if observation.stop_reason_type != "end_turn":
        result.failure_stage = "runtime"
        result.error = f"Unexpected stop reason: {observation.stop_reason_type or 'missing'}"
        return
    if observation.queue_wait_ms is None:
        result.failure_stage = "events"
        result.error = "Missing session.status_running event"
        return
    if observation.first_event_ms is None:
        result.failure_stage = "events"
        result.error = "Missing runtime event"
        return
    if not observation.response_contains_nonce:
        result.failure_stage = "response"
        result.error = "Agent response did not contain the smoke nonce"
        return

    threshold_failure = _threshold_failure(observation, config)
    if threshold_failure is not None:
        result.failure_stage = "threshold"
        result.error = threshold_failure
        return
    result.success = True


def _copy_observation(result: TurnResult, observation: _TurnObservation) -> None:
    """Preserve partial timing evidence even when monitoring later fails."""

    result.queue_wait_ms = observation.queue_wait_ms
    result.first_event_ms = observation.first_event_ms
    result.first_event_type = observation.first_event_type
    result.total_ms = observation.total_ms
    result.terminal_event_type = observation.terminal_event_type
    result.stop_reason_type = observation.stop_reason_type


def _threshold_failure(observation: _TurnObservation, config: SmokeConfig) -> str | None:
    checks = (
        ("queue wait", observation.queue_wait_ms, config.max_queue_wait),
        ("first event", observation.first_event_ms, config.max_first_event),
        ("total latency", observation.total_ms, config.max_total_latency),
    )
    for label, actual_ms, maximum_seconds in checks:
        if maximum_seconds is not None and actual_ms is not None and actual_ms > maximum_seconds * 1000:
            return f"{label} exceeded {maximum_seconds:.3f}s"
    return None


async def cleanup_owned_resources(
    client: Any,
    owned: OwnedResources,
    *,
    timeout: float,
    poll_interval: float,
    concurrency: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CleanupReport:
    """Delete only resources whose IDs were recorded as owned by this run."""

    report = CleanupReport()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def delete_session(session_id: str) -> None:
        async with semaphore:
            try:
                await _delete_session_with_retry(
                    client,
                    session_id,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    sleep=sleep,
                )
                report.deleted_sessions.append(session_id)
            except Exception as exc:
                report.errors.append(f"session {session_id}: {_safe_error(exc)}")

    await asyncio.gather(*(delete_session(session_id) for session_id in tuple(owned.session_ids)))

    # If a child Session could not be removed, retain its disposable parents so
    # the leaked Session is not left pointing at a deleted Environment.
    if len(report.deleted_sessions) != len(owned.session_ids):
        if owned.agent_id is not None or owned.environment_id is not None:
            report.errors.append("parent cleanup skipped because a Session could not be deleted")
        return report

    if owned.environment_id is not None:
        try:
            await client.environments.delete(owned.environment_id)
            report.deleted_environment = True
        except Exception as exc:
            if _status_code(exc) == 404:
                report.deleted_environment = True
            else:
                report.errors.append(f"environment {owned.environment_id}: {_safe_error(exc)}")
    if owned.agent_id is not None:
        try:
            await client.agents.archive(owned.agent_id)
            report.archived_agent = True
        except Exception as exc:
            if _status_code(exc) == 404:
                report.archived_agent = True
            else:
                report.errors.append(f"agent {owned.agent_id}: {_safe_error(exc)}")
    return report


async def _delete_session_with_retry(
    client: Any,
    session_id: str,
    *,
    timeout: float,
    poll_interval: float,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await client.sessions.delete(session_id)
            return
        except Exception as exc:
            if _status_code(exc) == 404:
                return
            last_error = exc
            if _status_code(exc) == 409:
                try:
                    await client.sessions.cancel(session_id)
                except Exception as cancel_exc:
                    if _status_code(cancel_exc) != 404:
                        last_error = cancel_exc
            await sleep(poll_interval)
    raise RuntimeError(f"Failed to delete smoke Session {session_id}") from last_error


def _is_runtime_event(event_type: str) -> bool:
    return bool(
        event_type
        and event_type not in STATUS_EVENTS
        and not event_type.startswith("user.")
        and not event_type.startswith("system.")
        and event_type not in {"session.updated", "session.deleted"}
    )


def _serialized_event(event: Any) -> str:
    if hasattr(event, "model_dump"):
        value = event.model_dump(mode="json")
    elif isinstance(event, dict):
        value = event
    else:
        value = vars(event)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _event_latency_ms(event: Any, accepted_at: datetime | None, fallback_ms: float) -> float:
    event_at = _as_datetime(_value(event, "created_at"))
    if accepted_at is None or event_at is None:
        return fallback_ms
    return max(0.0, (event_at - accepted_at).total_seconds() * 1000.0)


def _safe_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip() or type(exc).__name__
    return message[:500]


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    return value if isinstance(value, int) else None


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _metric_summary(results: Sequence[TurnResult], field_name: str) -> dict[str, float | int | None]:
    values = [float(value) for result in results if (value := getattr(result, field_name)) is not None]
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "p50": round(statistics.median(values), 2),
        "p95": round(_percentile(values, 0.95), 2),
        "max": round(max(values), 2),
    }


def _validate_config(config: SmokeConfig) -> None:
    if not config.vault_ids:
        raise ValueError("At least one Vault ID is required")
    if not 1 <= config.session_count <= 50:
        raise ValueError("session_count must be between 1 and 50")
    if not 1 <= config.provision_concurrency <= 10:
        raise ValueError("provision_concurrency must be between 1 and 10")
    if config.turn_timeout <= 0 or config.poll_interval <= 0 or config.cleanup_timeout <= 0:
        raise ValueError("timeouts and poll_interval must be positive")
    for name, value in (
        ("max_queue_wait", config.max_queue_wait),
        ("max_first_event", config.max_first_event),
        ("max_total_latency", config.max_total_latency),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive")


def _target_label(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or "unknown-host"
    if parsed.port is not None:
        return f"{host}:{parsed.port}"
    return host


def _validate_base_url(base_url: str, *, allow_insecure_http: bool) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must be an absolute http(s) URL")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and parsed.hostname not in local_hosts and not allow_insecure_http:
        raise ValueError("remote targets must use HTTPS; pass --allow-insecure-http only when intentional")


def _csv_env(*names: str) -> list[str]:
    for name in names:
        raw = os.environ.get(name)
        if raw:
            return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create disposable VMA Sessions, trigger concurrent turns, report latency, "
            "and clean up only resources created by this run."
        ),
        epilog=(
            "Set the API credential with VMA_PERF_API_KEY (preferred), "
            "VMA_SMOKE_API_KEY, VMA_API_KEY, or VOTRIX_VMA_API_KEY. It is "
            "intentionally not accepted "
            "as a command-line argument."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=_first_env(
            "VMA_PERF_BASE_URL",
            "VMA_SMOKE_BASE_URL",
            "VMA_BASE_URL",
            "VOTRIX_VMA_BASE_URL",
        ),
    )
    parser.add_argument(
        "--vault-id",
        action="append",
        dest="vault_ids",
        help="Existing Vault containing the model credential; repeat to preserve lookup order.",
    )
    parser.add_argument("--agent-id", default=os.environ.get("VMA_PERF_AGENT_ID"))
    parser.add_argument("--environment-id", default=os.environ.get("VMA_PERF_ENVIRONMENT_ID"))
    parser.add_argument("--provider", default=os.environ.get("VMA_PERF_MODEL_PROVIDER", DEFAULT_PROVIDER))
    parser.add_argument("--model", default=os.environ.get("VMA_PERF_MODEL", DEFAULT_MODEL))
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSION_COUNT)
    parser.add_argument("--provision-concurrency", type=int, default=2)
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--turn-timeout", type=float, default=900.0)
    parser.add_argument("--cleanup-timeout", type=float, default=90.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--max-queue-wait", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--max-first-event", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--max-total-latency", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _print_report(report: SmokeReport, *, target: str, json_output: bool) -> None:
    payload = report.to_dict(target=target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print(
        f"VMA performance smoke: {'PASS' if report.ok else 'FAIL'} "
        f"target={target} run={report.run_id} "
        f"passed={report.passed}/{report.requested_sessions}"
    )
    print("idx  session             provision  trigger  queue    first    total    result")
    for result in report.results:
        session = (result.session_id or "-")[:19]
        outcome = "pass" if result.success else f"fail:{result.failure_stage or 'setup'}"
        print(
            f"{result.index:>3}  {session:<19} "
            f"{_format_ms(result.provision_ms):>9} "
            f"{_format_ms(result.trigger_http_ms):>8} "
            f"{_format_ms(result.queue_wait_ms):>8} "
            f"{_format_ms(result.first_event_ms):>8} "
            f"{_format_ms(result.total_ms):>8}  {outcome}"
        )
        if result.error:
            print(f"     error: {result.error}")
    for name, values in payload["latency_ms"].items():
        if values["count"]:
            print(
                f"{name}: p50={values['p50']:.2f}ms "
                f"p95={values['p95']:.2f}ms max={values['max']:.2f}ms"
            )
    if report.setup_error:
        print(f"setup error: {report.setup_error}")
    if report.cleanup.errors:
        print("cleanup errors:")
        for error in report.cleanup.errors:
            print(f"  - {error}")
    else:
        print(f"cleanup: deleted {len(report.cleanup.deleted_sessions)} disposable Sessions")


def _format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}ms"


async def _main_async(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    api_key = _first_env(
        "VMA_PERF_API_KEY",
        "VMA_SMOKE_API_KEY",
        "VMA_API_KEY",
        "VOTRIX_VMA_API_KEY",
    )
    if not api_key:
        parser.error(
            "VMA_PERF_API_KEY, VMA_SMOKE_API_KEY, VMA_API_KEY, or "
            "VOTRIX_VMA_API_KEY is required"
        )
    if not args.base_url:
        parser.error(
            "--base-url, VMA_PERF_BASE_URL, VMA_SMOKE_BASE_URL, VMA_BASE_URL, "
            "or VOTRIX_VMA_BASE_URL is required"
        )
    try:
        _validate_base_url(args.base_url, allow_insecure_http=args.allow_insecure_http)
    except ValueError as exc:
        parser.error(str(exc))

    vault_ids = args.vault_ids or _csv_env("VMA_PERF_VAULT_IDS", "VMA_PERF_VAULT_ID")
    if not vault_ids:
        parser.error("--vault-id or VMA_PERF_VAULT_IDS is required")
    # Preserve caller order but prevent duplicate attachments.
    vault_ids = list(dict.fromkeys(vault_ids))
    config = SmokeConfig(
        vault_ids=tuple(vault_ids),
        session_count=args.sessions,
        agent_id=args.agent_id,
        environment_id=args.environment_id,
        provider=args.provider,
        model=args.model,
        provision_concurrency=args.provision_concurrency,
        turn_timeout=args.turn_timeout,
        poll_interval=args.poll_interval,
        cleanup_timeout=args.cleanup_timeout,
        max_queue_wait=args.max_queue_wait,
        max_first_event=args.max_first_event,
        max_total_latency=args.max_total_latency,
    )
    try:
        _validate_config(config)
    except ValueError as exc:
        parser.error(str(exc))

    target = _target_label(args.base_url)
    print(
        f"Starting disposable {config.session_count}-Session smoke against {target}; "
        "all created resources will be cleaned up.",
        file=sys.stderr,
    )
    async with AsyncVotrix(
        api_key=api_key,
        base_url=args.base_url,
        timeout=args.request_timeout,
        max_retries=2,
    ) as client:
        report = await run_smoke(client, config)
    _print_report(report, target=target, json_output=args.json_output)
    return 0 if report.ok else 1


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args, parser)))


if __name__ == "__main__":
    main()
