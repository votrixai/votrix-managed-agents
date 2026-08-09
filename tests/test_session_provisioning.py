"""Process-local protection for the expensive cold Session path."""

from __future__ import annotations

import asyncio

from app.services import sessions as service


async def test_only_four_sessions_provision_at_once(monkeypatch):
    gate = asyncio.Event()
    active = 0
    maximum = 0
    entered = asyncio.Event()

    async def _create(db, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == service.MAX_CONCURRENT_SESSION_PROVISIONS:
            entered.set()
        try:
            await gate.wait()
            return kwargs["agent_id"]
        finally:
            active -= 1

    monkeypatch.setattr(service, "_create_session", _create)
    monkeypatch.setattr(
        service,
        "_session_provision_slots",
        asyncio.Semaphore(service.MAX_CONCURRENT_SESSION_PROVISIONS),
    )

    tasks = [
        asyncio.create_task(
            service.create_session(
                None,
                organization_id="org_test",
                agent_id=f"agent_{index}",
                environment_id="env_test",
            )
        )
        for index in range(8)
    ]

    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert maximum == service.MAX_CONCURRENT_SESSION_PROVISIONS
    assert active == service.MAX_CONCURRENT_SESSION_PROVISIONS

    gate.set()
    assert await asyncio.gather(*tasks) == [f"agent_{index}" for index in range(8)]
    assert maximum == service.MAX_CONCURRENT_SESSION_PROVISIONS
