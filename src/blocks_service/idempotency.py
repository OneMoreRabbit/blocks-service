"""Idempotent replay of client operations.

Both clients queue operations offline and retry on reconnect, so the service must
apply each exactly once. The client supplies `Idempotency-Key`; the server stores
the outcome and replays it verbatim on a repeat.

The fingerprint is what makes this safe rather than merely convenient. A key
replayed with a *different* request body is a client bug -- two distinct
operations sharing one identifier -- and replaying the first response would
silently discard the second operation. That is refused with a conflict instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

import psycopg
from fastapi.responses import JSONResponse

from . import errors


def creation_identity(key: str | None, method: str, path: str, body: bytes) -> bytes:
    """The identity retained on a resource for the life of the resource.

    Covers the **key and the initial body** together (the resource id is the row it
    is stored on), so the fixed precedence rule reduces to one comparison:

        same id + same key + same initial body  -> resource replay
        any mismatch                            -> 409

    Deliberately a separate value from the `fingerprint()` stored in
    `idempotency_keys`. That one answers "what did this operation return" and may
    expire; this one answers "is this the same operation that created this
    resource" and must not. Folding the key in is what makes "the same key with a
    different id or body is always 409" true after expiry as well as before.
    """
    digest = hashlib.sha256()
    digest.update((key or "").encode())
    digest.update(b"\0")
    digest.update(fingerprint(method, path, body))
    return digest.digest()


def fingerprint(method: str, path: str, body: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(method.encode())
    digest.update(b"\0")
    digest.update(path.encode())
    digest.update(b"\0")
    digest.update(body)
    return digest.digest()


class Idempotency:
    """Replay-or-run, on the caller's already-tenant-scoped transaction.

    Sharing the transaction is the point: the stored outcome and the state change
    it describes commit together, so a crash can never leave a key claiming an
    operation that did not happen.
    """

    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        tenant_id: UUID,
        key: str | None,
        request_fingerprint: bytes,
    ) -> None:
        self._conn = conn
        self._tenant_id = tenant_id
        self._key = key
        self._fingerprint = request_fingerprint
        #: Set by the request dependency; retained on a resource so identity
        #: survives this record's expiry (ADR-0011).
        self.creation_identity: bytes | None = None

    async def run(
        self, status_code: int, produce: Callable[[], Awaitable[dict]]
    ) -> JSONResponse:
        """Reserve the key, then execute. Never the other way round.

        The ordering is the whole safety property. An earlier version executed
        `produce()` first and arbitrated afterwards, which is safe sequentially and
        wrong under simultaneous delivery: two requests sharing a key but not a
        body both found no result, both created a resource and an outbox event, and
        the loser rolled back only its *key* insert -- committing its extra domain
        writes and returning the winner's answer. One operation key, two resources,
        and nothing told the caller.

        Claiming first means at most one request is ever entitled to execute.
        PostgreSQL supplies the barrier: a second inserter of the same key blocks
        on the unique index until the first transaction commits or aborts, so it
        *learns* the outcome instead of racing it. A waiter therefore always reads
        a completed result -- a reservation that never completes was rolled back
        together with the work it was reserving.
        """
        if self._key is None:
            return JSONResponse(status_code=status_code, content=await produce())

        stored = await self._read()
        if stored is not None:
            return self._replay(stored)

        try:
            async with self._conn.transaction():   # savepoint: a lost claim is not fatal
                await self._conn.execute(
                    "insert into blocks.idempotency_keys (tenant_id, key, fingerprint)"
                    " values (%s, %s, %s)",
                    (self._tenant_id, self._key, self._fingerprint),
                )
        except psycopg.errors.UniqueViolation:
            # Someone else owns this key and has now committed. Re-read and decide
            # WITHOUT executing: that is the point of claiming first.
            stored = await self._read()
            if stored is None:
                raise
            return self._replay(stored)

        body = await produce()
        await self._conn.execute(
            "update blocks.idempotency_keys set status_code = %s, response = %s"
            " where key = %s and status_code is null",
            (status_code, json.dumps(body), self._key),
        )
        return JSONResponse(status_code=status_code, content=body)

    async def complete(self, status_code: int, body: dict) -> None:
        """Record the outcome against a reservation this request already holds.

        Needed because a handler can finish by a path that does not return through
        `run()` -- a resource replay raises past it. Without this the reservation
        would commit with no result, and the next retry of that key would read a
        row it cannot replay from.
        """
        if self._key is None:
            return
        await self._conn.execute(
            "update blocks.idempotency_keys set status_code = %s, response = %s"
            " where key = %s and status_code is null",
            (status_code, json.dumps(body), self._key),
        )

    async def _read(self) -> dict | None:
        cur = await self._conn.execute(
            "select fingerprint, status_code, response from blocks.idempotency_keys"
            " where key = %s",
            (self._key,),
        )
        return await cur.fetchone()

    def _replay(self, stored: dict) -> JSONResponse:
        """Replay a completed result, or refuse a different operation under one key.

        The fingerprint comparison happens here and only here, so a waiter cannot
        reach `produce()` by a path that skips it -- which is exactly how the
        previous version returned the winner's answer to a caller who had asked
        for something else.
        """
        if bytes(stored["fingerprint"]) != self._fingerprint:
            raise errors.conflict(
                "This Idempotency-Key was already used for a different request. "
                "Use a fresh key per distinct operation."
            )
        if stored["status_code"] is None:
            # Unreachable in practice: a reservation is only visible to another
            # transaction once its own committed, and that commit carries the
            # result. Refusing beats inventing a response if it ever happens.
            raise errors.conflict("that operation is still in progress; retry")
        return JSONResponse(
            status_code=stored["status_code"],
            content=stored["response"],
            headers={"Idempotent-Replay": "true"},
        )
