"""Actor-scoped, latest-only Project navigation hints (ADR-0030 proposal)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import psycopg

IMMEDIATE_MAX_AGE_MS = 10_000
LIFETIME_MS = 60_000
SOURCE_LIMIT = 32


@dataclass(frozen=True)
class FollowRefusal(Exception):
    status_code: int
    code: str
    reason: str
    message: str
    delivery: str | None = None
    current_revision: int | None = None

    def body(self) -> dict:
        detail: dict = {
            "code": self.code,
            "message": self.message,
            "reason": self.reason,
            "retryable": False,
        }
        if self.delivery is not None:
            detail["delivery"] = self.delivery
        if self.current_revision is not None:
            detail["current_revision"] = self.current_revision
        return {"error": detail}


def _rejected(
    reason: str, message: str, *, current_revision: int | None = None
) -> FollowRefusal:
    return FollowRefusal(
        409, "conflict", reason, message, "not_accepted", current_revision
    )


async def _current_status(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    user_id: UUID,
    source_id: UUID,
    kind: str,
    receipt_revision: int,
    expires_at: datetime,
) -> str:
    table = "follow_explicit" if kind == "explicit_button" else "follow_automatic"
    source_clause = "" if kind == "explicit_button" else " and source_device_id = %s"
    params = (
        (tenant_id, user_id)
        if kind == "explicit_button"
        else (tenant_id, user_id, source_id)
    )
    cur = await conn.execute(
        f"select revision from blocks.{table}"
        f" where tenant_id = %s and user_id = %s{source_clause}",
        params,
    )
    latest = await cur.fetchone()
    if latest is None or latest["revision"] != receipt_revision:
        return "superseded"
    cur = await conn.execute("select clock_timestamp() as t")
    now = (await cur.fetchone())["t"]
    return "expired" if expires_at <= now else "current"


async def publish(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    source_id: UUID,
    sequence: int,
    project_id: UUID,
    kind: str,
    dispatch_class: str,
    observed_revision: int | None,
    age_ms: int,
) -> dict:
    """Serialize acceptance per user; retries never restamp an accepted hint."""
    await conn.execute(
        "insert into blocks.follow_user_state (tenant_id, user_id) values (%s, %s)"
        " on conflict (tenant_id, user_id) do nothing",
        (tenant_id, user_id),
    )
    cur = await conn.execute(
        "select revision, current_source_id, run_start_revision"
        " from blocks.follow_user_state"
        " where tenant_id = %s and user_id = %s for update",
        (tenant_id, user_id),
    )
    state = await cur.fetchone()
    assert state is not None

    cur = await conn.execute(
        "select * from blocks.follow_sources"
        " where tenant_id = %s and user_id = %s and source_device_id = %s for update",
        (tenant_id, user_id, source_id),
    )
    source = await cur.fetchone()
    if source is not None:
        high_water = source["high_water_sequence"]
        if sequence == high_water:
            same = (
                source["last_project_id"] == project_id
                and source["last_kind"] == kind
                and source["last_dispatch_class"] == dispatch_class
                and source["last_observed_revision"] == observed_revision
            )
            if not same:
                raise FollowRefusal(
                    409,
                    "conflict",
                    "intent_conflict",
                    "This source sequence was already used for another intent.",
                )
            return {
                "revision": source["receipt_revision"],
                "accepted_at": source["accepted_at"],
                "expires_at": source["expires_at"],
                "replayed": True,
                "stream_status": await _current_status(
                    conn,
                    tenant_id,
                    user_id,
                    source_id,
                    kind,
                    source["receipt_revision"],
                    source["expires_at"],
                ),
            }
        if sequence < high_water:
            raise FollowRefusal(
                409,
                "conflict",
                "stale_sequence",
                "An accepted higher sequence already exists for this source.",
            )

    limit = IMMEDIATE_MAX_AGE_MS if dispatch_class == "immediate" else LIFETIME_MS
    if age_ms >= limit:
        raise FollowRefusal(
            400,
            "invalid_request",
            "intent_expired",
            "The original action is too old for a new signal.",
            "not_accepted",
        )

    cur = await conn.execute(
        "select id from blocks.projects where tenant_id = %s and id = %s"
        " and status = 'active'::blocks.project_status for share",
        (tenant_id, project_id),
    )
    if await cur.fetchone() is None:
        # The same answer for absent, archived, and another tenant's Project.
        raise FollowRefusal(
            404,
            "not_found",
            "target_unavailable",
            "No active Project is available for this selection.",
            "not_accepted",
        )

    current_revision = state["revision"]
    if dispatch_class == "deferred":
        assert observed_revision is not None
        current_source = state["current_source_id"] == source_id
        same_source_predecessor = (
            current_source
            and source is not None
            and source["high_water_sequence"] < sequence
            and state["run_start_revision"] <= observed_revision <= current_revision
        )
        if observed_revision != current_revision and not same_source_predecessor:
            raise _rejected(
                "stale_candidate",
                "The selection changed after this action.",
                current_revision=current_revision,
            )

    if source is None:
        cur = await conn.execute(
            "select count(*) as n from blocks.follow_sources"
            " where tenant_id = %s and user_id = %s",
            (tenant_id, user_id),
        )
        if (await cur.fetchone())["n"] >= SOURCE_LIMIT:
            raise _rejected(
                "source_limit_reached",
                "This account has reached the Follow source limit.",
            )

    cur = await conn.execute("select clock_timestamp() as t")
    accepted_at = (await cur.fetchone())["t"]
    expires_at = accepted_at + timedelta(milliseconds=LIFETIME_MS - age_ms)
    revision = current_revision + 1
    await conn.execute(
        "insert into blocks.follow_sources"
        " (tenant_id, user_id, source_device_id, high_water_sequence, last_project_id,"
        "  last_kind, last_dispatch_class, last_observed_revision, receipt_revision,"
        "  accepted_at, expires_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (tenant_id, user_id, source_device_id) do update set"
        " high_water_sequence = excluded.high_water_sequence,"
        " last_project_id = excluded.last_project_id,"
        " last_kind = excluded.last_kind,"
        " last_dispatch_class = excluded.last_dispatch_class,"
        " last_observed_revision = excluded.last_observed_revision,"
        " receipt_revision = excluded.receipt_revision,"
        " accepted_at = excluded.accepted_at, expires_at = excluded.expires_at",
        (
            tenant_id,
            user_id,
            source_id,
            sequence,
            project_id,
            kind,
            dispatch_class,
            observed_revision,
            revision,
            accepted_at,
            expires_at,
        ),
    )
    if kind == "explicit_button":
        await conn.execute(
            "insert into blocks.follow_explicit"
            " (tenant_id, user_id, source_device_id, project_id, revision,"
            " accepted_at, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict (tenant_id, user_id) do update set"
            " source_device_id = excluded.source_device_id,"
            " project_id = excluded.project_id,"
            " revision = excluded.revision, accepted_at = excluded.accepted_at,"
            " expires_at = excluded.expires_at",
            (
                tenant_id,
                user_id,
                source_id,
                project_id,
                revision,
                accepted_at,
                expires_at,
            ),
        )
    else:
        await conn.execute(
            "insert into blocks.follow_automatic"
            " (tenant_id, user_id, source_device_id, project_id, revision,"
            " accepted_at, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict (tenant_id, user_id, source_device_id) do update set"
            " project_id = excluded.project_id, revision = excluded.revision,"
            " accepted_at = excluded.accepted_at, expires_at = excluded.expires_at",
            (
                tenant_id,
                user_id,
                source_id,
                project_id,
                revision,
                accepted_at,
                expires_at,
            ),
        )
    run_start = (
        state["run_start_revision"]
        if state["current_source_id"] == source_id
        else current_revision
    )
    await conn.execute(
        "update blocks.follow_user_state set revision = %s, current_source_id = %s,"
        " run_start_revision = %s where tenant_id = %s and user_id = %s",
        (revision, source_id, run_start, tenant_id, user_id),
    )
    return {
        "revision": revision,
        "accepted_at": accepted_at,
        "expires_at": expires_at,
        "replayed": False,
        "stream_status": "current",
    }


async def read_selection(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    automatic_source_device_id: UUID | None,
) -> dict:
    cur = await conn.execute(
        "select revision from blocks.follow_user_state"
        " where tenant_id = %s and user_id = %s",
        (tenant_id, user_id),
    )
    state = await cur.fetchone()
    current_revision = state["revision"] if state else 0
    cur = await conn.execute(
        "select source_device_id, project_id, revision, accepted_at, expires_at"
        " from blocks.follow_explicit where tenant_id = %s and user_id = %s",
        (tenant_id, user_id),
    )
    explicit = await cur.fetchone()
    automatic = None
    if automatic_source_device_id is not None:
        cur = await conn.execute(
            "select source_device_id, project_id, revision, accepted_at, expires_at"
            " from blocks.follow_automatic where tenant_id = %s and user_id = %s"
            " and source_device_id = %s",
            (tenant_id, user_id, automatic_source_device_id),
        )
        automatic = await cur.fetchone()
    if explicit is None and automatic is None:
        return {"current_revision": current_revision, "selection": None}
    kind = "explicit_button"
    chosen = explicit
    if automatic is not None and (
        chosen is None or automatic["revision"] > chosen["revision"]
    ):
        chosen = automatic
        kind = "automatic_nfc"
    cur = await conn.execute(
        "select 1 from blocks.projects where tenant_id = %s and id = %s"
        " and status = 'active'::blocks.project_status",
        (tenant_id, chosen["project_id"]),
    )
    if await cur.fetchone() is None:
        # A newer inaccessible hint must not reveal its target or fall back to
        # an older hint that would navigate in the wrong direction.
        return {"current_revision": current_revision, "selection": None}
    cur = await conn.execute("select clock_timestamp() as t")
    if chosen["expires_at"] <= (await cur.fetchone())["t"]:
        return {"current_revision": current_revision, "selection": None}
    return {
        "current_revision": current_revision,
        "selection": {
            "revision": chosen["revision"],
            "source_device_id": chosen["source_device_id"],
            "intent_kind": kind,
            "project_id": chosen["project_id"],
            "accepted_at": chosen["accepted_at"],
            "expires_at": chosen["expires_at"],
        },
    }
