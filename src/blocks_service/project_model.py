"""The project model (ADR-0009).

Projects are what the product is about: freely browsable, active or archived,
each with one explicitly-saved notes document and optional immutable parking.

What is *absent* here is the point. There is no current project, no pickup, no
resume, no close, no outcome tier, no calibration, and no one-active-session
rule. Opening a project is navigation, not a mutation -- physical-device UAT
rejected the workflow that made it one.

Every function runs on a connection already scoped to a tenant, so none carries a
tenant predicate; see `db.py` for why that is the design rather than an omission.
"""

from __future__ import annotations

from uuid import UUID

import psycopg

from . import events
from .operating_loop import ConcurrencyConflict, IllegalTransition, NotFound

PROJECT_COLUMNS = (
    "id, tenant_id, name, status::text as status, row_version,"
    " created_at, updated_at, archived_at"
)


async def list_projects(conn: psycopg.AsyncConnection, status: str = "active") -> list[dict]:
    """The dashboard. Active by default; archived is a separate, deliberate ask."""
    cur = await conn.execute(
        f"select {PROJECT_COLUMNS} from blocks.projects"
        " where status = %s::blocks.project_status order by created_at desc",
        (status,),
    )
    return list(await cur.fetchall())


async def get_project(conn: psycopg.AsyncConnection, project_id: UUID) -> dict:
    cur = await conn.execute(
        f"select {PROJECT_COLUMNS} from blocks.projects where id = %s", (project_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no such project in this tenant")
    return row


async def create_project(conn: psycopg.AsyncConnection, tenant_id: UUID, name: str) -> dict:
    """Create a project. Its empty notes document arrives with it.

    The notes row is a database invariant (`projects_ensure_notes`, migration
    0008) rather than something this function remembers to do. That is deliberate:
    during the window between migration and image cutover the pre-0.3 writer is
    still live and inserts projects without notes, so the guarantee has to hold
    for writers this code knows nothing about.
    """
    cur = await conn.execute(
        "insert into blocks.projects (tenant_id, name) values (%s, %s)"
        f" returning {PROJECT_COLUMNS}",
        (tenant_id, name),
    )
    row = await cur.fetchone()
    assert row is not None
    # The notes row is created by the `projects_ensure_notes` trigger (0008), not
    # here. One place owns the invariant, and it is the place that also covers the
    # pre-0.3 writer still running between migration and cutover.
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="project.created",
        payload={"projectId": str(row["id"])},
    )
    return row


async def _update_project(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    expected_version: int,
    *,
    assignments: str,
    params: tuple,
    event_type: str,
) -> dict:
    cur = await conn.execute(
        f"update blocks.projects set {assignments},"
        " row_version = row_version + 1, updated_at = now()"
        " where id = %s and row_version = %s"
        f" returning {PROJECT_COLUMNS}",
        (*params, project_id, expected_version),
    )
    row = await cur.fetchone()
    if row is None:
        raise ConcurrencyConflict(
            "project was modified by someone else, or does not exist in this tenant"
        )
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type=event_type,
        payload={"projectId": str(project_id)},
    )
    return row


async def rename_project(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID,
    expected_version: int, name: str,
) -> dict:
    return await _update_project(
        conn, tenant_id, project_id, expected_version,
        assignments="name = %s", params=(name,), event_type="project.renamed",
    )


async def set_project_status(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID,
    expected_version: int, status: str,
) -> dict:
    """Archive or reopen. Reversible, and never a delete.

    Archiving hides a project from the dashboard; reopening restores it. Nothing
    is removed, which is why this is a status change rather than a destructive
    operation with a friendly name.
    """
    current = await get_project(conn, project_id)
    if current["status"] == status:
        raise IllegalTransition(f"project is already {status}")
    return await _update_project(
        conn, tenant_id, project_id, expected_version,
        assignments="status = %s::blocks.project_status,"
                    " archived_at = case when %s = 'archived' then now() else null end",
        params=(status, status),
        event_type="project.archived" if status == "archived" else "project.reopened",
    )


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


async def get_notes(conn: psycopg.AsyncConnection, project_id: UUID) -> dict:
    cur = await conn.execute(
        "select project_id, body, row_version, updated_at from blocks.project_notes"
        " where project_id = %s",
        (project_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no such project in this tenant")
    return row


async def save_notes(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID,
    expected_version: int, body: str,
) -> dict:
    """Explicitly saved, never autosaved.

    An explicit save is what makes optimistic concurrency meaningful: the client
    states the version it edited, so two devices editing the same notes produce a
    conflict a person can resolve rather than a silent last-writer-wins.
    """
    cur = await conn.execute(
        "update blocks.project_notes set body = %s,"
        " row_version = row_version + 1, updated_at = now()"
        " where project_id = %s and row_version = %s"
        " returning project_id, body, row_version, updated_at",
        (body, project_id, expected_version),
    )
    row = await cur.fetchone()
    if row is None:
        raise ConcurrencyConflict(
            "notes were modified by someone else, or the project does not exist here"
        )
    # Identifiers and routing metadata only -- never the note body.
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="project.notes_saved",
        payload={"projectId": str(project_id), "rowVersion": row["row_version"]},
    )
    return row


# ---------------------------------------------------------------------------
# Parking, now belonging to a project
# ---------------------------------------------------------------------------

PARKING_COLUMNS = (
    "id, project_id, where_i_am, next_physical_action, must_not_forget,"
    " authored_by_user_id, device_id, supersedes_id, created_at"
)


async def append_parking(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    *,
    actor_id: UUID,
    where_i_am: str,
    next_physical_action: str,
    must_not_forget: str,
    device_id: UUID | None = None,
    supersedes_id: UUID | None = None,
) -> dict:
    """Append a park message. Optional, immutable, newest-first.

    Corrections append a superseding record rather than editing one; the table
    grants `blocks_app` no UPDATE at all, so that is enforced rather than agreed.
    """
    await get_project(conn, project_id)  # 404 rather than a foreign-key error
    if supersedes_id is not None:
        cur = await conn.execute(
            "select 1 from blocks.parking_records where id = %s and project_id = %s",
            (supersedes_id, project_id),
        )
        if await cur.fetchone() is None:
            raise NotFound("the record being superseded is not in this project")

    cur = await conn.execute(
        "select coalesce(max(sequence), 0) + 1 as n from blocks.parking_records"
        " where project_id = %s",
        (project_id,),
    )
    sequence = (await cur.fetchone())["n"]

    cur = await conn.execute(
        "insert into blocks.parking_records"
        " (tenant_id, project_id, where_i_am, next_physical_action, must_not_forget,"
        "  authored_by_user_id, device_id, sequence, supersedes_id)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        f" returning {PARKING_COLUMNS}",
        (
            tenant_id, project_id, where_i_am, next_physical_action, must_not_forget,
            actor_id, device_id, sequence, supersedes_id,
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="project.parked",
        payload={"projectId": str(project_id), "parkingRecordId": str(row["id"])},
    )
    return row


async def newest_parking(conn: psycopg.AsyncConnection, project_id: UUID) -> dict | None:
    """The newest record that nothing supersedes. What the project page shows."""
    cur = await conn.execute(
        f"select {PARKING_COLUMNS} from blocks.parking_records p"
        " where p.project_id = %s"
        "   and not exists ("
        "     select 1 from blocks.parking_records s where s.supersedes_id = p.id"
        "   )"
        " order by p.created_at desc, p.id desc limit 1",
        (project_id,),
    )
    return await cur.fetchone()


async def parking_history(conn: psycopg.AsyncConnection, project_id: UUID) -> list[dict]:
    """Every record, newest first, including superseded ones.

    Ordered by created_at rather than `sequence`: sequence was unique per work
    session, and legacy rows migrated from two sessions of one project can repeat
    a number. The column is retained as history, not used as an ordering key.
    """
    cur = await conn.execute(
        f"select {PARKING_COLUMNS} from blocks.parking_records"
        " where project_id = %s order by created_at desc, id desc",
        (project_id,),
    )
    return list(await cur.fetchall())
