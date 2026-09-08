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
        if self._key is None:
            return JSONResponse(status_code=status_code, content=await produce())

        cur = await self._conn.execute(
            "select fingerprint, status_code, response from blocks.idempotency_keys"
            " where key = %s",
            (self._key,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            if bytes(existing["fingerprint"]) != self._fingerprint:
                raise errors.conflict(
                    "This Idempotency-Key was already used for a different request. "
                    "Use a fresh key per distinct operation."
                )
            return JSONResponse(
                status_code=existing["status_code"],
                content=existing["response"],
                headers={"Idempotent-Replay": "true"},
            )

        body = await produce()
        await self._conn.execute(
            "insert into blocks.idempotency_keys"
            " (tenant_id, key, fingerprint, status_code, response)"
            " values (%s, %s, %s, %s, %s)",
            (self._tenant_id, self._key, self._fingerprint, status_code, json.dumps(body)),
        )
        return JSONResponse(status_code=status_code, content=body)
