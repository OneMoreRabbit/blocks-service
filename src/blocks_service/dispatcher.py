"""The outbox dispatcher: the worker half of the component.

Events are written to `blocks.outbox` in the same transaction as the state change
that produced them, so a committed change always has its event. This process is
what turns those rows into deliveries.

It connects as `blocks_dispatcher`, whose privilege is settled by ADR-0005: two
role-scoped RLS policies give it cross-tenant *visibility* of the outbox and
nothing else, and a column-level `UPDATE` grant bounds its *authority* to the
delivery bookkeeping column. It cannot move an event between tenants, forge an
event's type, version or payload, insert or delete rows, or read any other table.
It holds no `BYPASSRLS`.

**Delivery is at-least-once.** A batch is claimed, delivered, and marked in one
transaction; if the process dies after delivering and before committing, those
events are delivered again. Consumers deduplicate on `eventId`, which the
published envelope schema documents as stable across redeliveries. The
alternative -- mark first, deliver after -- turns a crash into silent event loss,
which is worse and much harder to notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from typing import Protocol

import psycopg

from .config import Settings, get_settings
from .db import Database

log = logging.getLogger("blocks.dispatcher")

#: One dispatcher at a time. SKIP LOCKED already makes concurrent dispatchers
#: safe -- two would never claim the same row -- but they would interleave
#: deliveries, and nothing downstream has asked for that yet. A single holder
#: keeps per-tenant delivery in outbox order until there is a reason to relax it.
DISPATCH_LOCK_KEY = 4242424243


class SingleInstanceViolation(RuntimeError):
    """Another dispatcher already holds the lock."""


class EventSink(Protocol):
    async def deliver(self, envelope: dict) -> None: ...


class LoggingSink:
    """Writes the envelope to the log and nothing else.

    The only sink that exists, because no consumer exists: the service-mediated
    device event channel is not built. This is deliberately not a stub that
    pretends to deliver -- it makes the outbox drain observable, and the moment a
    real transport lands it replaces this one behind the same Protocol.
    """

    async def deliver(self, envelope: dict) -> None:
        log.info(
            "event dispatched",
            extra={"event": {k: v for k, v in envelope.items() if k != "payload"}},
        )


class NullSink:
    """Discards. For tests that care about bookkeeping rather than delivery."""

    async def deliver(self, envelope: dict) -> None:
        return None


class RecordingSink:
    """Keeps what it was given. Test double."""

    def __init__(self) -> None:
        self.delivered: list[dict] = []

    async def deliver(self, envelope: dict) -> None:
        self.delivered.append(envelope)


class FailingSink:
    """Always raises, to prove a failed delivery is not marked dispatched."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("delivery failed")
        self.attempts = 0

    async def deliver(self, envelope: dict) -> None:
        self.attempts += 1
        raise self.exc


def envelope_for(row: dict) -> dict:
    """Assemble the published envelope from an outbox row.

    Shape and field names come from `contracts/events/blocks-event-envelope-v0_1
    .schema.json`, which is the artefact clients build against; the test suite
    validates what this produces against that file.
    """
    return {
        "eventId": str(row["id"]),
        "eventType": row["event_type"],
        "eventVersion": row["event_version"],
        "tenantId": str(row["tenant_id"]),
        "occurredAt": row["occurred_at"].isoformat(),
        "payload": row["payload"],
    }


class Dispatcher:
    def __init__(
        self,
        db: Database,
        sink: EventSink,
        *,
        batch_size: int = 100,
        poll_seconds: float = 1.0,
    ) -> None:
        self._db = db
        self._sink = sink
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds

    async def dispatch_batch(self) -> int:
        """Claim, deliver, and mark one batch. Returns how many were dispatched.

        Everything happens inside one transaction. The claim uses `FOR UPDATE
        SKIP LOCKED`, which holds row locks for its duration -- so a second
        dispatcher passes over these rows instead of blocking on them, and a
        crash releases them for redelivery rather than stranding them.

        `order by id` is a global ordering, which also preserves per-tenant
        ordering: ids are monotonic, so a tenant's events keep their relative
        order regardless of how they interleave with other tenants'.
        """
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "select id, tenant_id, event_type, event_version, payload, occurred_at"
                " from blocks.outbox"
                " where dispatched_at is null"
                " order by id"
                " for update skip locked"
                " limit %s",
                (self._batch_size,),
            )
            rows = await cur.fetchall()
            if not rows:
                return 0

            for row in rows:
                # Delivery inside the transaction is what makes this at-least-once:
                # a raise here rolls back the claim, so nothing is marked and the
                # batch is retried. It also means a slow sink holds row locks --
                # acceptable while the sink is local, and the first thing to
                # revisit when a real transport with network latency arrives.
                await self._sink.deliver(envelope_for(row))

            await conn.execute(
                "update blocks.outbox set dispatched_at = now() where id = any(%s)",
                ([row["id"] for row in rows],),
            )
            return len(rows)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Drain, then poll. Exits promptly when `stop` is set."""
        while not stop.is_set():
            try:
                dispatched = await self.dispatch_batch()
            except Exception:
                log.exception("dispatch batch failed; retrying after poll interval")
                dispatched = 0
            if dispatched:
                log.info("dispatched %d event(s)", dispatched)
                continue  # drain greedily before sleeping again
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)


@contextlib.asynccontextmanager
async def single_instance(db: Database):
    """Hold the dispatcher advisory lock for the duration, or refuse to run."""
    async with db.transaction() as conn:
        cur = await conn.execute("select pg_try_advisory_lock(%s) as got", (DISPATCH_LOCK_KEY,))
        row = await cur.fetchone()
        if not row or not row["got"]:
            raise SingleInstanceViolation(
                "another dispatcher holds the advisory lock; refusing to start a second"
            )
    try:
        yield
    finally:
        async with db.transaction() as conn:
            await conn.execute("select pg_advisory_unlock(%s)", (DISPATCH_LOCK_KEY,))


async def main(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db = Database(settings.dispatcher_dsn, min_size=1, max_size=2)
    await db.open()
    try:
        # The same assertion the API's readiness probe makes. The dispatcher sees
        # every tenant's events by policy, and that is only safe while it cannot
        # also bypass the policies.
        await db.assert_cannot_bypass_rls()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        async with single_instance(db):
            dispatcher = Dispatcher(
                db,
                LoggingSink(),
                batch_size=settings.dispatch_batch_size,
                poll_seconds=settings.dispatch_poll_seconds,
            )
            log.info("dispatcher started")
            await dispatcher.run_forever(stop)
        log.info("dispatcher stopped cleanly")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
