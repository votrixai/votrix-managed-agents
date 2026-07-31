#!/usr/bin/env python3
"""Measure what this database costs from where you are sitting.

Self-contained: it imports nothing from this repository, writes nothing to the
database, and reads no application table. Copy the file anywhere, give it a
connection string, run it.

    pip install asyncpg            # sqlalchemy too, for the last section
    export DATABASE_URL='postgresql://...'
    python db-latency-probe.py

    # or
    python db-latency-probe.py 'postgresql://...'

Why it exists
-------------
Every query against our Supabase instance costs about the same whether it
reads one row or counts a whole table, which means the time is not the
database doing work — it is the network. This measures that directly, so the
question "is our database slow, or are we just far away from it?" gets an
answer instead of an opinion.

Send back the whole output. It contains no credentials: the connection string
is printed with its user and password removed.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import statistics
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROUNDS = 10


# --- presentation ------------------------------------------------------------


def summarise(samples: list[float]) -> str:
    """Mean first, because that is what gets compared between two machines.

    The median is printed beside it and the two should agree. Where they do
    not, one round went long — a TLS renegotiation, a scheduler hiccup — and
    the mean is the one being misled. Min and max are there so that is
    visible rather than inferred.
    """
    if not samples:
        return "no samples"
    return (
        f"n={len(samples):<3} mean={statistics.fmean(samples):7.1f}  "
        f"med={statistics.median(samples):7.1f}  "
        f"min={min(samples):7.1f}  max={max(samples):7.1f}  ms"
    )


def line(label: str, samples: list[float]) -> None:
    print(f"  {label:<34}{summarise(samples)}")


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def safe_url(url: str) -> str:
    """The connection string with the credentials taken out."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def timed(function, *args, **kwargs) -> float:
    started = time.monotonic()
    function(*args, **kwargs)
    return (time.monotonic() - started) * 1000


async def timed_async(coroutine_factory) -> float:
    started = time.monotonic()
    await coroutine_factory()
    return (time.monotonic() - started) * 1000


# --- the four things being separated ----------------------------------------


def measure_tcp(host: str, port: int) -> list[float]:
    """A bare TCP handshake. The floor: no database can beat this."""
    samples = []
    for _ in range(ROUNDS):
        try:
            samples.append(timed(lambda: socket.create_connection((host, port), timeout=15).close()))
        except OSError as exc:
            print(f"  TCP connect failed: {exc}")
            return samples
    return samples


def measure_tls(host: str, port: int) -> list[float]:
    """TCP plus a TLS handshake, spoken to the host directly.

    Not the Postgres handshake — Postgres negotiates TLS after a plaintext
    preamble — but close enough to show what the certificate exchange costs on
    this route, and it isolates it from authentication.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    samples = []
    for _ in range(ROUNDS):
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=15) as raw:
                try:
                    with context.wrap_socket(raw, server_hostname=host):
                        pass
                except (ssl.SSLError, OSError):
                    # Postgres on 5432 does not answer a raw TLS ClientHello.
                    # The TCP part still happened; report what we have.
                    return samples
        except OSError:
            return samples
        samples.append((time.monotonic() - started) * 1000)
    return samples


async def measure_connect(dsn: str) -> list[float]:
    """A full Postgres connection: TCP, TLS, and authentication."""
    import asyncpg

    samples = []
    for _ in range(ROUNDS):
        started = time.monotonic()
        connection = await asyncpg.connect(dsn, statement_cache_size=0)
        samples.append((time.monotonic() - started) * 1000)
        await connection.close()
    return samples


async def measure_statements(dsn: str, cache_size: int) -> dict[str, list[float]]:
    """Queries on a connection that is already open.

    Three queries doing very different amounts of work. If they all cost the
    same, the database is not the thing being measured.
    """
    import asyncpg

    queries = {
        "SELECT 1": "SELECT 1",
        "SELECT now()": "SELECT now()",
        "count(*) pg_class": "SELECT count(*) FROM pg_class",
    }
    results: dict[str, list[float]] = {}
    connection = await asyncpg.connect(dsn, statement_cache_size=cache_size)
    try:
        for label, query in queries.items():
            samples = []
            for _ in range(ROUNDS):
                samples.append(await timed_async(lambda q=query: connection.fetch(q)))
            results[label] = samples
    finally:
        await connection.close()
    return results


async def measure_pools(url: str) -> None:
    """One session plus one query, with and without connection pooling.

    This is the shape the application actually uses: open a session, run
    something, close it. Without a pool that means a new connection every time.
    """
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool
    except ImportError:
        print("  (skipped: sqlalchemy is not installed)")
        return

    async_url = url
    if async_url.startswith("postgresql://"):
        async_url = async_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif async_url.startswith("postgres://"):
        async_url = async_url.replace("postgres://", "postgresql+asyncpg://", 1)

    for label, options in (
        ("NullPool", {"poolclass": NullPool}),
        (
            "QueuePool(size=5)",
            {"poolclass": AsyncAdaptedQueuePool, "pool_size": 5, "max_overflow": 5},
        ),
        (
            "QueuePool + pre_ping",
            {
                "poolclass": AsyncAdaptedQueuePool,
                "pool_size": 5,
                "max_overflow": 5,
                "pool_pre_ping": True,
            },
        ),
    ):
        engine = create_async_engine(
            async_url,
            connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
            **options,
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        samples = []
        for _ in range(ROUNDS):
            started = time.monotonic()
            async with factory() as db:
                await db.execute(text("SELECT 1"))
            samples.append((time.monotonic() - started) * 1000)
        # The first is a cold start every time and drowns the rest.
        line(f"{label} (first {samples[0]:.0f}ms)", samples[1:])
        await engine.dispose()


# --- report ------------------------------------------------------------------


def url_from_dotenv() -> str:
    """Last resort, so running this beside a checkout needs no shell ceremony.

    Deliberately not `python-dotenv`: this file has to run on a laptop that
    has asyncpg and nothing else.
    """
    for candidate in (".env", "../.env"):
        try:
            with open(candidate) as handle:
                for row in handle:
                    key, _, value = row.partition("=")
                    if key.strip() == "DATABASE_URL":
                        return value.strip().strip("'\"")
        except OSError:
            continue
    return ""


async def main() -> int:
    url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_URL") or url_from_dotenv()
    )
    if not url:
        print(__doc__)
        print("error: pass a connection string, or set DATABASE_URL")
        return 2

    dsn = url
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if dsn.startswith(prefix):
            dsn = dsn.replace(prefix, "postgresql://", 1)

    parts = urlsplit(dsn)
    host, port = parts.hostname, parts.port or 5432
    if not host:
        print(f"error: no host in {safe_url(url)}")
        return 2

    print("database latency probe")
    print(f"  target   {safe_url(url)}")
    print(f"  client   {socket.gethostname()}")
    try:
        print(f"  resolves {socket.gethostbyname(host)}")
    except OSError:
        pass
    print(f"  rounds   {ROUNDS}")

    heading("1. network floor — TCP handshake, nothing else")
    tcp = measure_tcp(host, port)
    line("TCP connect", tcp)
    tls = measure_tls(host, port)
    if tls:
        line("TCP + TLS", tls)

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        print("\nerror: asyncpg is not installed — pip install asyncpg")
        return 2

    heading("2. full Postgres connection — TCP + TLS + auth")
    connects = await measure_connect(dsn)
    line("asyncpg.connect", connects)
    if tcp and connects:
        overhead = statistics.median(connects) - statistics.median(tcp)
        print(f"  {'TLS + auth on top of TCP':<34}{overhead:7.1f} ms")

    heading("3. queries on an already-open connection")
    print("  If these three are the same, the cost is the network, not the query.\n")
    print("  prepared-statement cache OFF (what the service configures today)")
    off = await measure_statements(dsn, 0)
    for label, samples in off.items():
        line(f"  {label}", samples)

    print("\n  prepared-statement cache ON")
    try:
        on = await measure_statements(dsn, 100)
        for label, samples in on.items():
            line(f"  {label}", samples)
        saved = statistics.median(off["SELECT 1"]) - statistics.median(on["SELECT 1"])
        print(f"\n  {'saving per repeated query':<34}{saved:7.1f} ms")
    except Exception as exc:
        # Expected against a pooler in transaction mode, and worth knowing.
        print(f"    failed: {type(exc).__name__}: {exc}")
        print("    (this pooler does not support prepared statements)")

    heading("4. one session + one query, by pooling strategy")
    await measure_pools(dsn)

    heading("summary (means over 10 rounds)")
    if tcp:
        print(f"  one network round trip is roughly {statistics.fmean(tcp) / 1.5:.0f} ms")
    if connects:
        print(f"  every unpooled connection costs   {statistics.fmean(connects):.0f} ms")
    if off:
        print(f"  every query costs                 {statistics.fmean(off['SELECT 1']):.0f} ms")
    print("\n  Send this whole output back — it contains no credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
