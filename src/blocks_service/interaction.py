"""Ordered aggregates and Park tombstones (ADR-0024 and ADR-0028)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

import psycopg

from . import events
from .operating_loop import ConcurrencyConflict, DomainError, NotFound


class InvalidInteraction(DomainError):
    """The complete operation envelope is not valid for the aggregate."""


class InteractionConflict(ConcurrencyConflict):
    """An operation identity or aggregate version conflicts."""


def _fingerprint(envelope: dict) -> bytes:
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).digest()


async def _receipt(conn: psycopg.AsyncConnection, operation_id: UUID) -> dict | None:
    cur = await conn.execute(
        "select envelope_fingerprint, result from blocks.interaction_operations"
        " where operation_id = %s",
        (operation_id,),
    )
    return await cur.fetchone()


def _replay(receipt: dict, fingerprint: bytes) -> dict:
    if bytes(receipt["envelope_fingerprint"]) != fingerprint:
        raise InteractionConflict(
            "that operation_id is already bound to a different complete envelope"
        )
    return receipt["result"]


async def _record(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    operation_id: UUID,
    operation_type: str,
    aggregate_id: UUID,
    project_id: UUID | None,
    target_id: UUID | None,
    base_version: int,
    fingerprint: bytes,
    result: dict,
) -> None:
    await conn.execute(
        "insert into blocks.interaction_operations"
        " (tenant_id, operation_id, operation_type, aggregate_id, project_id, target_id,"
        "  base_version, envelope_fingerprint, result)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            tenant_id, operation_id, operation_type, aggregate_id, project_id,
            target_id, base_version, fingerprint,
            json.dumps(result, sort_keys=True, separators=(",", ":")),
        ),
    )


async def project_order(conn: psycopg.AsyncConnection, tenant_id: UUID) -> dict:
    cur = await conn.execute(
        "select aggregate_id, version, last_operation_id"
        " from blocks.project_order_aggregates where tenant_id = %s",
        (tenant_id,),
    )
    aggregate = await cur.fetchone()
    if aggregate is None:
        cur = await conn.execute(
            "insert into blocks.project_order_aggregates (tenant_id) values (%s)"
            " returning aggregate_id, version, last_operation_id",
            (tenant_id,),
        )
        aggregate = await cur.fetchone()
    cur = await conn.execute(
        "select p.client_id from blocks.project_order_positions o"
        " join blocks.projects p on p.tenant_id = o.tenant_id and p.id = o.project_id"
        " where o.tenant_id = %s order by o.position",
        (tenant_id,),
    )
    rows = list(await cur.fetchall())
    if any(row["client_id"] is None for row in rows):
        raise InvalidInteraction(
            "active Project ordering requires a client_id for every active Project"
        )
    return {
        "operation_id": aggregate["last_operation_id"],
        "aggregate_id": aggregate["aggregate_id"],
        "ordered_client_ids": [str(row["client_id"]) for row in rows],
        "version": aggregate["version"],
    }


async def link_order(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID
) -> dict:
    cur = await conn.execute(
        "select aggregate_id, version, last_operation_id"
        " from blocks.link_order_aggregates where project_id = %s",
        (project_id,),
    )
    aggregate = await cur.fetchone()
    if aggregate is None:
        raise NotFound("no such project in this tenant")
    cur = await conn.execute(
        "select link_id from blocks.link_order_positions"
        " where project_id = %s order by position",
        (project_id,),
    )
    return {
        "operation_id": aggregate["last_operation_id"],
        "aggregate_id": aggregate["aggregate_id"],
        "project_id": project_id,
        "ordered_client_ids": [str(row["link_id"]) for row in await cur.fetchall()],
        "version": aggregate["version"],
    }


async def note_order(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID
) -> dict:
    cur = await conn.execute(
        "select aggregate_id, version, last_operation_id"
        " from blocks.note_order_aggregates where project_id = %s",
        (project_id,),
    )
    aggregate = await cur.fetchone()
    if aggregate is None:
        raise NotFound("no such project in this tenant")
    cur = await conn.execute(
        "select note_id from blocks.note_order_positions"
        " where project_id = %s order by position",
        (project_id,),
    )
    return {
        "operation_id": aggregate["last_operation_id"],
        "aggregate_id": aggregate["aggregate_id"],
        "project_id": project_id,
        "ordered_client_ids": [str(row["note_id"]) for row in await cur.fetchall()],
        "version": aggregate["version"],
    }


async def reorder_projects(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    *,
    operation_id: UUID,
    aggregate_id: UUID,
    base_version: int,
    ordered_client_ids: list[UUID],
) -> tuple[dict, bool]:
    envelope = {
        "operation_id": str(operation_id), "type": "REORDER_PROJECTS",
        "aggregate_id": str(aggregate_id), "base_version": base_version,
        "ordered_client_ids": [str(value) for value in ordered_client_ids],
    }
    fingerprint = _fingerprint(envelope)
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True

    cur = await conn.execute(
        "select version from blocks.project_order_aggregates"
        " where tenant_id = %s and aggregate_id = %s for update",
        (tenant_id, aggregate_id),
    )
    aggregate = await cur.fetchone()
    if aggregate is None:
        raise NotFound("no such active-Project order aggregate in this tenant")
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True
    if aggregate["version"] != base_version:
        raise InteractionConflict("active-Project order version is stale; read and reconcile")
    if len(set(ordered_client_ids)) != len(ordered_client_ids):
        raise InvalidInteraction("ordered_client_ids must contain every live member exactly once")
    cur = await conn.execute(
        "select client_id from blocks.projects where status = 'active'"
    )
    members = [row["client_id"] for row in await cur.fetchall()]
    if any(member is None for member in members) or set(members) != set(ordered_client_ids):
        raise InvalidInteraction("ordered_client_ids must contain every live member exactly once")

    await conn.execute(
        "delete from blocks.project_order_positions where tenant_id = %s", (tenant_id,)
    )
    if ordered_client_ids:
        await conn.execute(
            "insert into blocks.project_order_positions (tenant_id, project_id, position)"
            " select %s, p.id, u.position from unnest(%s::uuid[]) with ordinality u(client_id, position)"
            " join blocks.projects p on p.client_id = u.client_id",
            (tenant_id, ordered_client_ids),
        )
    version = base_version + 1
    await conn.execute(
        "update blocks.project_order_aggregates"
        " set version = %s, last_operation_id = %s, updated_at = now()"
        " where tenant_id = %s",
        (version, operation_id, tenant_id),
    )
    result = {
        "operation_id": str(operation_id), "aggregate_id": str(aggregate_id),
        "ordered_client_ids": [str(value) for value in ordered_client_ids],
        "version": version,
    }
    await _record(
        conn, tenant_id=tenant_id, operation_id=operation_id,
        operation_type="REORDER_PROJECTS", aggregate_id=aggregate_id,
        project_id=None, target_id=None, base_version=base_version,
        fingerprint=fingerprint, result=result,
    )
    await events.emit(
        conn, tenant_id=tenant_id, event_type="project.order_changed",
        payload={"aggregateId": str(aggregate_id), "version": version,
                 "operationId": str(operation_id)},
    )
    return result, False


async def reorder_links(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    *,
    operation_id: UUID,
    aggregate_id: UUID,
    base_version: int,
    ordered_client_ids: list[UUID],
) -> tuple[dict, bool]:
    envelope = {
        "operation_id": str(operation_id), "type": "REORDER_LINKS",
        "aggregate_id": str(aggregate_id), "project_id": str(project_id),
        "base_version": base_version,
        "ordered_client_ids": [str(value) for value in ordered_client_ids],
    }
    fingerprint = _fingerprint(envelope)
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True
    cur = await conn.execute(
        "select version from blocks.link_order_aggregates"
        " where project_id = %s and aggregate_id = %s for update",
        (project_id, aggregate_id),
    )
    aggregate = await cur.fetchone()
    if aggregate is None:
        raise NotFound("no such Link order aggregate in this project")
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True
    if aggregate["version"] != base_version:
        raise InteractionConflict("Link order version is stale; read and reconcile")
    if len(set(ordered_client_ids)) != len(ordered_client_ids):
        raise InvalidInteraction("ordered_client_ids must contain every live member exactly once")
    cur = await conn.execute(
        "select id from blocks.project_resource_links"
        " where project_id = %s and deleted_at is null",
        (project_id,),
    )
    members = {row["id"] for row in await cur.fetchall()}
    if members != set(ordered_client_ids):
        raise InvalidInteraction("ordered_client_ids must contain every live member exactly once")

    await conn.execute(
        "delete from blocks.link_order_positions where project_id = %s", (project_id,)
    )
    if ordered_client_ids:
        await conn.execute(
            "insert into blocks.link_order_positions (tenant_id, project_id, link_id, position)"
            " select %s, %s, u.link_id, u.position"
            " from unnest(%s::uuid[]) with ordinality u(link_id, position)",
            (tenant_id, project_id, ordered_client_ids),
        )
    version = base_version + 1
    await conn.execute(
        "update blocks.link_order_aggregates"
        " set version = %s, last_operation_id = %s, updated_at = now()"
        " where project_id = %s",
        (version, operation_id, project_id),
    )
    result = {
        "operation_id": str(operation_id), "aggregate_id": str(aggregate_id),
        "project_id": str(project_id),
        "ordered_client_ids": [str(value) for value in ordered_client_ids],
        "version": version,
    }
    await _record(
        conn, tenant_id=tenant_id, operation_id=operation_id,
        operation_type="REORDER_LINKS", aggregate_id=aggregate_id,
        project_id=project_id, target_id=None, base_version=base_version,
        fingerprint=fingerprint, result=result,
    )
    await events.emit(
        conn, tenant_id=tenant_id, event_type="project.link_order_changed",
        payload={"aggregateId": str(aggregate_id), "projectId": str(project_id),
                 "version": version, "operationId": str(operation_id)},
    )
    return result, False


async def reorder_notes(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    *,
    operation_id: UUID,
    aggregate_id: UUID,
    base_version: int,
    ordered_client_ids: list[UUID],
) -> tuple[dict, bool]:
    envelope = {
        "operation_id": str(operation_id), "type": "REORDER_NOTES",
        "aggregate_id": str(aggregate_id), "project_id": str(project_id),
        "base_version": base_version,
        "ordered_client_ids": [str(value) for value in ordered_client_ids],
    }
    fingerprint = _fingerprint(envelope)
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True
    cur = await conn.execute(
        "select version from blocks.note_order_aggregates"
        " where project_id = %s and aggregate_id = %s for update",
        (project_id, aggregate_id),
    )
    aggregate = await cur.fetchone()
    if aggregate is None:
        raise NotFound("no such Note order aggregate in this project")
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True
    if aggregate["version"] != base_version:
        raise InteractionConflict("Note order version is stale; read and reconcile")
    if len(set(ordered_client_ids)) != len(ordered_client_ids):
        raise InvalidInteraction("ordered_client_ids must contain every live member exactly once")
    cur = await conn.execute(
        "select id from blocks.project_note_items"
        " where project_id = %s and deleted_at is null",
        (project_id,),
    )
    members = {row["id"] for row in await cur.fetchall()}
    if members != set(ordered_client_ids):
        raise InvalidInteraction("ordered_client_ids must contain every live member exactly once")

    await conn.execute(
        "delete from blocks.note_order_positions where project_id = %s", (project_id,)
    )
    if ordered_client_ids:
        await conn.execute(
            "insert into blocks.note_order_positions (tenant_id, project_id, note_id, position)"
            " select %s, %s, u.note_id, u.position"
            " from unnest(%s::uuid[]) with ordinality u(note_id, position)",
            (tenant_id, project_id, ordered_client_ids),
        )
    version = base_version + 1
    await conn.execute(
        "update blocks.note_order_aggregates"
        " set version = %s, last_operation_id = %s, updated_at = now()"
        " where project_id = %s",
        (version, operation_id, project_id),
    )
    result = {
        "operation_id": str(operation_id), "aggregate_id": str(aggregate_id),
        "project_id": str(project_id),
        "ordered_client_ids": [str(value) for value in ordered_client_ids],
        "version": version,
    }
    await _record(
        conn, tenant_id=tenant_id, operation_id=operation_id,
        operation_type="REORDER_NOTES", aggregate_id=aggregate_id,
        project_id=project_id, target_id=None, base_version=base_version,
        fingerprint=fingerprint, result=result,
    )
    await events.emit(
        conn, tenant_id=tenant_id, event_type="project.note_order_changed",
        payload={"aggregateId": str(aggregate_id), "projectId": str(project_id),
                 "version": version, "operationId": str(operation_id)},
    )
    return result, False


async def delete_parking(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    parking_id: UUID,
    *,
    operation_id: UUID,
    aggregate_id: UUID,
    target_id: UUID,
    base_version: int,
) -> tuple[dict, bool]:
    envelope = {
        "operation_id": str(operation_id), "type": "DELETE_PARKING",
        "aggregate_id": str(aggregate_id), "project_id": str(project_id),
        "target_id": str(target_id), "base_version": base_version,
    }
    fingerprint = _fingerprint(envelope)
    if target_id != parking_id or aggregate_id != parking_id:
        raise InvalidInteraction("aggregate_id and target_id must name the Park in the path")
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True

    cur = await conn.execute(
        "select id, row_version, deleted_at from blocks.parking_records"
        " where id = %s and project_id = %s for update",
        (parking_id, project_id),
    )
    row = await cur.fetchone()
    if receipt := await _receipt(conn, operation_id):
        return _replay(receipt, fingerprint), True

    changed = False
    if row is None:
        result = {
            "operation_id": str(operation_id), "aggregate_id": str(aggregate_id),
            "project_id": str(project_id), "target_id": str(target_id),
            "version": base_version, "deleted_at": None, "changed": False,
        }
    else:
        if row["deleted_at"] is not None:
            raise InteractionConflict("Park is already tombstoned by another operation")
        if row["row_version"] != base_version:
            raise InteractionConflict("Park version is stale; read and reconcile")
        deleted_at = datetime.now(timezone.utc)
        version = base_version + 1
        await conn.execute(
            "update blocks.parking_records"
            " set deleted_at = %s, row_version = %s, last_operation_id = %s"
            " where id = %s and project_id = %s",
            (deleted_at, version, operation_id, parking_id, project_id),
        )
        changed = True
        result = {
            "operation_id": str(operation_id), "aggregate_id": str(aggregate_id),
            "project_id": str(project_id), "target_id": str(target_id),
            "version": version, "deleted_at": deleted_at.isoformat(), "changed": True,
        }
    await _record(
        conn, tenant_id=tenant_id, operation_id=operation_id,
        operation_type="DELETE_PARKING", aggregate_id=aggregate_id,
        project_id=project_id, target_id=target_id, base_version=base_version,
        fingerprint=fingerprint, result=result,
    )
    if changed:
        await events.emit(
            conn, tenant_id=tenant_id, event_type="project.parking_deleted",
            payload={"projectId": str(project_id), "parkingRecordId": str(parking_id),
                     "version": result["version"], "operationId": str(operation_id)},
        )
    return result, False


async def parking_tombstones(
    conn: psycopg.AsyncConnection, project_id: UUID
) -> list[dict]:
    cur = await conn.execute(
        "select id as target_id, project_id, row_version as version, deleted_at,"
        " last_operation_id as operation_id"
        " from blocks.parking_records where project_id = %s and deleted_at is not null"
        " order by deleted_at, id",
        (project_id,),
    )
    return list(await cur.fetchall())
