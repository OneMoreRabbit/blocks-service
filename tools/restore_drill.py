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

Two roles, deliberately. `blocks_backup` reads and dumps (BYPASSRLS,
`pg_read_all_data`, no write grants); `blocks_test_admin` creates and drops the
scratch database (CREATEDB, no bypass). Neither can do the other's job, which is
the point: nothing in this drill runs as a role that could both see everything
and change it.

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


def assert_can_dump(dsn: str) -> None:
    """A logical dump of this schema needs a role exempt from RLS. Check first.

    `pg_dump` sets `row_security = off` and errors if any table it reads is under
    a policy the role is subject to -- correct behaviour, because the alternative
    is a partial dump that looks complete.

    Do NOT "fix" that error with `--enable-row-security`. Measured on this schema:
    it exits 0, `pg_restore` exits 0, and every tenant-scoped table comes back
    EMPTY while the un-policied identity tables restore normally. A backup that
    succeeds and silently contains no tenant data is the worst available outcome.
    """
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
    if not row or not (row["rolsuper"] or row["rolbypassrls"]):
        raise DrillFailure(
            "the dumping role cannot bypass row-level security, so pg_dump cannot "
            "produce a complete dump of this schema. Use a dedicated backup role "
            "with BYPASSRLS, or a physical backup. Do NOT add --enable-row-security: "
            "it exits 0 and silently omits every tenant-scoped row."
        )


def seat_secret(name: str) -> str | None:
    path = pathlib.Path.home() / ".secrets" / name
    return path.read_text().strip() if path.exists() else None


def snapshot(dsn: str) -> dict:
    """The facts a restore must preserve, security first.

    Read as `blocks_backup`, which bypasses RLS, so tenant-scoped tables can be
    counted **grouped by tenant** rather than in total. That is the stronger
    check: a restore that returned the right number of rows attributed to the
    wrong tenants would pass a total and fail this.
    """
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
        # Row counts have to be taken per tenant now. No role in the estate can
        # bypass RLS -- deliberately -- so there is no connection from which a
        # global count of a tenant-scoped table is even meaningful: it would
        # read 0 and compare equal to another 0, and prove nothing.
        counts = {}
        for table in ("users", "tenants"):  # identity plane, no RLS
            counts[table] = conn.execute(f"select count(*) as n from blocks.{table}").fetchone()["n"]
        for table in TENANT_TABLES:
            rows = conn.execute(
                f"select tenant_id, count(*) as n from blocks.{table}"
                " group by tenant_id order by tenant_id"
            ).fetchall()
            for row in rows:
                counts[f"{table}@{row['tenant_id']}"] = row["n"]
    return {
        "migrations": [tuple(r.values()) for r in migrations],
        "rls": {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rls},
        "policies": sorted((r["tablename"], r["policyname"]) for r in policies),
        "grants": sorted((r["table_name"], r["privilege_type"]) for r in grants),
        "counts": counts,
    }


#: The privilege shape ADR-0005 decided. Asserted as an exact set, not a subset:
#: the failure worth catching is a LATER widening -- a policy or grant added by
#: anyone, for any reason -- which a "contains" check would wave through.
EXPECTED_OUTBOX_POLICIES = {
    "outbox_tenant_isolation",
    "outbox_dispatcher_read",
    "outbox_dispatcher_mark",
}
EXPECTED_DISPATCHER_TABLE_GRANTS = {("outbox", "SELECT")}
#: WRITE privileges only. information_schema.column_privileges expands a
#: table-level SELECT into one row per column, so an unfiltered set would be
#: dominated by reads that the table-grant check already covers. What must be
#: exact is what the dispatcher can WRITE.
EXPECTED_DISPATCHER_COLUMN_WRITES = {("outbox", "dispatched_at", "UPDATE")}
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "REFERENCES")


def verify_dispatcher_privilege(dsn: str) -> None:
    """The dispatcher's exemption is declared, so it can be checked. Check it.

    A role attribute could not have been verified this way -- that is the
    argument ADR-0005 made, and this is the part of it that has to be kept true.
    """
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        policies = {
            r["policyname"]
            for r in conn.execute(
                "select policyname from pg_policies"
                " where schemaname = 'blocks' and tablename = 'outbox'"
            ).fetchall()
        }
        table_grants = {
            (r["table_name"], r["privilege_type"])
            for r in conn.execute(
                "select table_name, privilege_type from information_schema.table_privileges"
                " where table_schema = 'blocks' and grantee = 'blocks_dispatcher'"
            ).fetchall()
        }
        column_writes = {
            (r["table_name"], r["column_name"], r["privilege_type"])
            for r in conn.execute(
                "select table_name, column_name, privilege_type"
                " from information_schema.column_privileges"
                " where table_schema = 'blocks' and grantee = 'blocks_dispatcher'"
                "   and privilege_type = any(%s)",
                (list(WRITE_PRIVILEGES),),
            ).fetchall()
        }
        role = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = 'blocks_dispatcher'"
        ).fetchone()

    if policies != EXPECTED_OUTBOX_POLICIES:
        raise DrillFailure(
            f"blocks.outbox policies are {sorted(policies)}, expected "
            f"{sorted(EXPECTED_OUTBOX_POLICIES)} (ADR-0005)"
        )
    if table_grants != EXPECTED_DISPATCHER_TABLE_GRANTS:
        raise DrillFailure(
            f"blocks_dispatcher table grants are {sorted(table_grants)}, expected "
            f"{sorted(EXPECTED_DISPATCHER_TABLE_GRANTS)} (ADR-0005)"
        )
    if column_writes != EXPECTED_DISPATCHER_COLUMN_WRITES:
        raise DrillFailure(
            f"blocks_dispatcher writable columns are {sorted(column_writes)}, expected "
            f"{sorted(EXPECTED_DISPATCHER_COLUMN_WRITES)} (ADR-0005)"
        )
    if role and (role["rolsuper"] or role["rolbypassrls"]):
        raise DrillFailure("blocks_dispatcher holds SUPERUSER or BYPASSRLS (ADR-0005)")
    print("  ok     dispatcher privilege matches ADR-0005 exactly")


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
    parser.add_argument(
        "--admin-dsn",
        default=os.environ.get("BLOCKS_TEST_ADMIN_DSN") or os.environ.get("BLOCKS_ADMIN_DSN"),
        help="a CREATEDB role that owns the scratch database; NOT a superuser",
    )
    parser.add_argument(
        "--dump-dsn",
        default=os.environ.get("BLOCKS_BACKUP_DSN"),
        help="the read-only, RLS-exempt role that takes the dump (default: blocks_backup)",
    )
    parser.add_argument("--source-db", default=os.environ.get("BLOCKS_DB_NAME", "blocks"))
    parser.add_argument("--scratch-db", default="blocks_drill")
    parser.add_argument("--app-user", default=os.environ.get("BLOCKS_DB_APP_USER", "blocks_app"))
    parser.add_argument("--app-password", default=os.environ.get("BLOCKS_DB_APP_PASSWORD", ""))
    parser.add_argument("--keep", action="store_true", help="do not drop the scratch database")
    args = parser.parse_args()

    if not args.admin_dsn:
        print("BLOCKS_TEST_ADMIN_DSN (or --admin-dsn) is required", file=sys.stderr)
        return 2

    dump_dsn = args.dump_dsn
    if not dump_dsn and (password := seat_secret("blocks_backup_password")):
        dump_dsn = as_role(swap_db(args.admin_dsn, args.source_db), "blocks_backup", password)
    if not dump_dsn:
        print(
            "no dump role: set BLOCKS_BACKUP_DSN or place the blocks_backup password at "
            "~/.secrets/blocks_backup_password",
            file=sys.stderr,
        )
        return 2

    source_dsn = swap_db(dump_dsn, args.source_db)
    scratch_dsn = swap_db(dump_dsn, args.scratch_db)
    admin_scratch_dsn = swap_db(args.admin_dsn, args.scratch_db)
    cli, env = dsn_to_args(source_dsn)
    admin_cli, admin_env = dsn_to_args(args.admin_dsn)

    try:
        print(f"drill: {args.source_db} -> {args.scratch_db}")
        assert_can_dump(source_dsn)
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
                    " to blocks_owner, blocks_migrator, blocks_app, blocks_backup"
                )

            # Restored by the admin role, not the dump role: blocks_backup has no
            # write grants at all, which is exactly why it is safe to hold BYPASSRLS.
            run(
                ["pg_restore", *admin_cli, "-d", args.scratch_db, "--exit-on-error", str(dump)],
                env=admin_env,
            )
            print("  ok     pg_restore --exit-on-error")

        after = snapshot(scratch_dsn)
        compare(before, after)
        verify_dispatcher_privilege(admin_scratch_dsn)
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
