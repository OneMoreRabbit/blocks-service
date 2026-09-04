"""Domain events and the outbox append.

The schemas in `contracts/events/` are the published artefact, not documentation
generated after the fact: `emit` stamps the version those files declare, and the
test suite validates every emitted payload against them. A payload that drifts
from its published schema fails the build rather than reaching a client.
"""

from __future__ import annotations

import json
import pathlib
from typing import Final
from uuid import UUID

import psycopg

CONTRACTS: Final = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "events"

#: eventType -> the MAJOR.MINOR its payload schema is published at.
EVENT_VERSIONS: Final[dict[str, str]] = {
    # 0.2 dropped the project `name`: identifiers only (architecture ruling,
    # 2026-09-04). 0.1 is preserved on disk and still resolvable, because rows
    # already in the outbox are stamped 0.1 and must stay readable against the
    # schema they were written against -- which is the entire reason the version
    # is stamped per row rather than per deployment.
    "project.created": "0.2",
    "block.bound": "0.1",
    "block.unbound": "0.1",
    "session.started": "0.1",
    "session.parked": "0.1",
    "session.resumed": "0.1",
    "session.closed": "0.1",
}


class UnknownEventType(KeyError):
    """An event type with no published schema. Emitting one is a bug, not a warning."""


def schema_path(event_type: str, version: str) -> pathlib.Path:
    stem = event_type.replace(".", "-")
    return CONTRACTS / f"{stem}-v{version.replace('.', '_')}.schema.json"


def load_schema(event_type: str, version: str | None = None) -> dict:
    """The schema for an event type, at the CURRENT version unless one is named.

    A consumer reading history passes the version stamped on the row. Defaulting
    to the current version would validate an old payload against a newer shape
    and call correct history a defect.
    """
    return json.loads(schema_path(event_type, version or EVENT_VERSIONS[event_type]).read_text())


def known_versions(event_type: str) -> list[str]:
    """Every published version of an event's payload schema, oldest first."""
    stem = event_type.replace(".", "-")
    found = []
    for path in sorted(CONTRACTS.glob(f"{stem}-v*.schema.json")):
        # `.stem` leaves ".schema" on a double extension, so take the name apart
        # explicitly rather than trusting it.
        version = path.name[len(stem) + 2 : -len(".schema.json")]
        found.append(version.replace("_", "."))
    return found


async def emit(
    conn: psycopg.AsyncConnection, *, tenant_id: UUID, event_type: str, payload: dict
) -> None:
    """Append an event to the outbox on the caller's transaction.

    Takes the connection rather than opening one, because the entire guarantee is
    that this insert commits with the state change or not at all.
    """
    try:
        version = EVENT_VERSIONS[event_type]
    except KeyError as exc:
        raise UnknownEventType(event_type) from exc

    await conn.execute(
        "insert into blocks.outbox (tenant_id, event_type, event_version, payload)"
        " values (%s, %s, %s, %s)",
        (tenant_id, event_type, version, json.dumps(payload)),
    )
