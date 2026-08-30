#!/usr/bin/env python3
"""Forward-only SQL migration runner.

Plain versioned SQL rather than a migration framework, because most of what this
schema does -- roles, grants, policies, FORCE ROW LEVEL SECURITY -- is exactly
what ORM autogeneration models poorly and silently drops. The tradeoff is that
migrations are written by hand and are not reversible; rollback is a new forward
migration, which is also what a production deployment can actually do.

  python tools/migrate.py            apply pending migrations
  python tools/migrate.py --status   show applied/pending, verify checksums
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from blocks_service.config import get_settings  # noqa: E402

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"

# Any advisory-lock key works provided every deployer uses the same one; a
# literal keeps it stable across releases where a hashed name would not be.
LOCK_KEY = 4242424242

LEDGER = """
create schema if not exists blocks authorization blocks_owner;
create table if not exists blocks.schema_migrations (
  version     text primary key,
  checksum    text        not null,
  applied_at  timestamptz not null default now(),
  applied_by  text        not null default current_user
);
"""


def discover() -> list[tuple[str, pathlib.Path]]:
    return sorted(((p.stem, p) for p in MIGRATIONS.glob("*.sql")), key=lambda x: x[0])


def checksum(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="report without applying")
    parser.add_argument("--dsn", default=None, help="override the migrator DSN")
    args = parser.parse_args()

    settings = get_settings()
    dsn = args.dsn or settings.migrator_dsn

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        # Objects must belong to blocks_owner, not to whichever role happened to
        # run the migration, or ownership drifts by deployer.
        conn.execute("set role blocks_owner")
        conn.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute(LEDGER)
            cur = conn.execute("select version, checksum from blocks.schema_migrations")
            applied = {r["version"]: r["checksum"] for r in cur.fetchall()}

            drifted = [
                v for v, p in discover() if v in applied and applied[v] != checksum(p)
            ]
            if drifted:
                print(f"FAIL: already-applied migrations were edited: {', '.join(drifted)}")
                print("      An applied migration is history. Add a new one instead.")
                return 2

            pending = [(v, p) for v, p in discover() if v not in applied]

            if args.status:
                for v, _ in discover():
                    print(f"  {'applied' if v in applied else 'pending':>8}  {v}")
                print(f"{len(applied)} applied, {len(pending)} pending")
                return 0

            if not pending:
                print(f"up to date ({len(applied)} applied)")
                return 0

            for version, path in pending:
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute(
                        "insert into blocks.schema_migrations (version, checksum)"
                        " values (%s, %s)",
                        (version, checksum(path)),
                    )
                print(f"  applied  {version}")
            print(f"{len(pending)} migration(s) applied")
            return 0
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))


if __name__ == "__main__":
    raise SystemExit(main())
