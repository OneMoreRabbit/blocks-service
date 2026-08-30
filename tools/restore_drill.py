#!/usr/bin/env python3
"""Prove a backup can actually be restored -- including its security.

The deployment contract says restore is not proven until a drill has restored
into a scratch database and the service has passed readiness against it. This is
that drill, written as a tool rather than only a test so the orchestrator (which
owns backup and restore) can run the same check against a real backup.

What makes it worth running is not that the rows come back. It is that row-level
security comes back. A restore that returns every row but silently drops the
policies, the FORCE flag, or the grants looks like a complete success and is a
total tenant-isolation failure. Every check below exists because it is a way for
a restore to pass and still be wrong.

  python tools/restore_drill.py --source-db blocks --scratch-db blocks_drill
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

TENANT_TABLES = ("memberships", "projects", "outbox")


class DrillFailure(Exception):
    pass


def run(cmd: list[str], *, env: dict | None = None) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise DrillFailure(f"{cmd[0]} failed ({result.returncode}):\n{result.stderr.strip()}")


def swap_db(dsn: str, dbname: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    return " ".join([*parts, f"dbname={dbname}"])


def as_role(dsn: str, user: str, password: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith(("user=", "password="))]
    parts.append(f"user={user}")
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


def dsn_to_args(dsn: str) -> tuple[list[str], dict]:
    """psycopg keyword DSN -> libpq CLI flags, with the password out of argv."""
    fields = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
    args = []
    for key, flag in (("host", "-h"), ("port", "-p"), ("user", "-U")):
        if key in fields:
            args += [flag, fields[key]]
    env = {**os.environ}
    if "password" in fields:
        env["PGPASSWORD"] = fields["password"]
    return args, env


def snapshot(dsn: str) -> dict:
    """The facts a restore must preserve, security first."""
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        migrations = conn.execute(
            "select version, checksum from blocks.schema_migrations order by version"
        ).fetchall()
        rls = conn.execute(
            "select c.relname, c.relrowsecurity, c.relforcerowsecurity"
            " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'blocks' and c.relkind = 'r' order by c.relname"
        ).fetchall()
        policies = conn.execute(
            "select tablename, policyname from pg_policies"
            " where schemaname = 'blocks' order by tablename, policyname"
        ).fetchall()
        grants = conn.execute(
            "select table_name, privilege_type from information_schema.table_privileges"
            " where table_schema = 'blocks' and grantee = 'blocks_app'"
            " order by table_name, privilege_type"
        ).fetchall()
        counts = {}
        for table in ("users", "tenants", *TENANT_TABLES):
            counts[table] = conn.execute(f"select count(*) as n from blocks.{table}").fetchone()["n"]
    return {
        "migrations": [tuple(r.values()) for r in migrations],
        "rls": {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rls},
        "policies": sorted((r["tablename"], r["policyname"]) for r in policies),
        "grants": sorted((r["table_name"], r["privilege_type"]) for r in grants),
        "counts": counts,
    }


def verify_isolation(app_dsn: str) -> None:
    """Restore is only a success if the restored copy still isolates tenants."""
    with psycopg.connect(app_dsn, row_factory=dict_row, autocommit=True) as conn:
        role = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        if role["rolsuper"] or role["rolbypassrls"]:
            raise DrillFailure("restored-into role can bypass RLS; isolation unproven")

        unscoped = conn.execute("select count(*) as n from blocks.projects").fetchone()["n"]
        if unscoped != 0:
            # Note the failure this actually catches. A restore that dropped the
            # POLICY while leaving RLS enabled denies everything -- loud, and
            # caught by compare(). The silent one is RLS itself not surviving:
            # every row is then visible to every tenant and nothing looks wrong.
            raise DrillFailure(
                f"{unscoped} project rows visible with NO tenant context after restore -- "
                "row-level security did not survive"
            )

        with conn.transaction():
            tenants = conn.execute(
                "select id from blocks.tenants order by created_at"
            ).fetchall()
        if len(tenants) < 2:
            print("  note   fewer than 2 tenants restored; cross-tenant probe skipped")
            return

        seen = []
        for row in tenants[:2]:
            with conn.transaction():
                conn.execute(
                    "select set_config('blocks.tenant_id', %s, true)", (str(row["id"]),)
                )
                visible = conn.execute(
                    "select coalesce(array_agg(tenant_id::text), '{}') as t from blocks.projects"
                ).fetchone()["t"]
            foreign = [t for t in visible if UUID(t) != row["id"]]
            if foreign:
                raise DrillFailure(f"tenant {row['id']} saw rows from {foreign} after restore")
            seen.append(len(visible))
        print(f"  ok     cross-tenant probe: two tenants saw only their own rows {seen}")


def compare(before: dict, after: dict) -> None:
    for table in TENANT_TABLES:
        enabled, forced = after["rls"].get(table, (False, False))
        if not enabled or not forced:
            raise DrillFailure(
                f"blocks.{table} restored with rowsecurity={enabled} force={forced}; "
                "expected both true"
            )
    print(f"  ok     RLS enabled AND forced on {', '.join(TENANT_TABLES)}")

    for key, label in (
        ("migrations", "migration ledger"),
        ("policies", "policies"),
        ("grants", "blocks_app grants"),
        ("counts", "row counts"),
    ):
        if before[key] != after[key]:
            raise DrillFailure(f"{label} differ after restore:\n  before {before[key]}\n  after  {after[key]}")
        print(f"  ok     {label} identical after restore")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-dsn", default=os.environ.get("BLOCKS_ADMIN_DSN"))
    parser.add_argument("--source-db", default=os.environ.get("BLOCKS_DB_NAME", "blocks"))
    parser.add_argument("--scratch-db", default="blocks_drill")
    parser.add_argument("--app-user", default=os.environ.get("BLOCKS_DB_APP_USER", "blocks_app"))
    parser.add_argument("--app-password", default=os.environ.get("BLOCKS_DB_APP_PASSWORD", ""))
    parser.add_argument("--keep", action="store_true", help="do not drop the scratch database")
    args = parser.parse_args()

    if not args.admin_dsn:
        print("BLOCKS_ADMIN_DSN (or --admin-dsn) is required", file=sys.stderr)
        return 2

    source_dsn = swap_db(args.admin_dsn, args.source_db)
    scratch_dsn = swap_db(args.admin_dsn, args.scratch_db)
    cli, env = dsn_to_args(args.admin_dsn)

    try:
        print(f"drill: {args.source_db} -> {args.scratch_db}")
        before = snapshot(source_dsn)

        with tempfile.TemporaryDirectory() as tmp:
            dump = pathlib.Path(tmp) / "blocks.dump"
            # Custom format, PostgreSQL-aware, as the deployment contract requires:
            # a filesystem copy of a running data directory is not a backup.
            run(["pg_dump", *cli, "-d", args.source_db, "-Fc", "-f", str(dump)], env=env)
            print(f"  ok     pg_dump -Fc ({dump.stat().st_size:,} bytes)")

            with psycopg.connect(args.admin_dsn, autocommit=True) as conn:
                conn.execute(f'drop database if exists "{args.scratch_db}" with (force)')
                conn.execute(f'create database "{args.scratch_db}"')
                # Database-level GRANTs live in the cluster's globals, NOT in a
                # single-database dump. A real restore therefore needs
                # `pg_dumpall --globals-only` as well, or the restored database
                # is correct and unreachable. Reproduced here so the drill fails
                # for real reasons rather than for a missing CONNECT.
                conn.execute(
                    f'grant connect on database "{args.scratch_db}"'
                    " to blocks_owner, blocks_migrator, blocks_app"
                )

            run(["pg_restore", *cli, "-d", args.scratch_db, "--exit-on-error", str(dump)], env=env)
            print("  ok     pg_restore --exit-on-error")

        after = snapshot(scratch_dsn)
        compare(before, after)
        # Reconnect as the RUNTIME role, not the admin one the restore ran as:
        # the question is whether the restored copy isolates the service, and an
        # admin connection could not answer it either way.
        verify_isolation(as_role(scratch_dsn, args.app_user, args.app_password))

        print("drill: PASSED")
        return 0
    except DrillFailure as exc:
        print(f"drill: FAILED -- {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            with psycopg.connect(args.admin_dsn, autocommit=True) as conn:
                conn.execute(f'drop database if exists "{args.scratch_db}" with (force)')


if __name__ == "__main__":
    raise SystemExit(main())
