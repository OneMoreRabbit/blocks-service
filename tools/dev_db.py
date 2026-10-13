#!/usr/bin/env python3
"""Stand up a Blocks database on a development seat.

Applies tools/bootstrap_roles.sql -- the same file an operator would run through
psql -- and then the migrations. It exists so a seat with no psql client can
still reach a byte-identical database, and so bootstrap_roles.sql has exactly one
home rather than a Python paraphrase that can drift from it.

Role bootstrap needs CREATEROLE, which no Blocks seat has and none should: roles
are cluster-level and the orchestrator provisions them from the same
bootstrap_roles.sql. Pass --migrations-only on a seat.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

import psycopg
from psycopg import sql

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROLES_SQL = ROOT / "tools" / "bootstrap_roles.sql"

#: psql's :'var' (quoted literal) and :"var" (quoted identifier).
VAR = re.compile(r""":(?P<q>['"])(?P<name>\w+)(?P=q)""")


def render(text: str, variables: dict[str, str]) -> str:
    """Expand psql-style variables using psycopg's own quoting."""

    def replace(m: re.Match[str]) -> str:
        name = m.group("name")
        if name not in variables:
            raise KeyError(f"bootstrap_roles.sql references undefined variable :{name}")
        value = variables[name]
        node = sql.Identifier(value) if m.group("q") == '"' else sql.Literal(value)
        return node.as_string(None)

    # \set and psql meta-commands are directives to psql, not SQL.
    body = "\n".join(l for l in text.splitlines() if not l.startswith("\\"))
    return VAR.sub(replace, body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-dsn",
        default=os.environ.get("BLOCKS_ADMIN_DSN"),
        help="administrator DSN for role bootstrap; defaults to $BLOCKS_ADMIN_DSN",
    )
    parser.add_argument("--migrator-password", default=os.environ.get("BLOCKS_DB_MIGRATOR_PASSWORD"))
    parser.add_argument("--app-password", default=os.environ.get("BLOCKS_DB_APP_PASSWORD"))
    parser.add_argument("--dispatcher-password", default=os.environ.get("BLOCKS_DB_DISPATCHER_PASSWORD"))
    parser.add_argument("--backup-password", default=os.environ.get("BLOCKS_DB_BACKUP_PASSWORD"))
    parser.add_argument("--test-admin-password", default=os.environ.get("BLOCKS_TEST_ADMIN_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("BLOCKS_DB_NAME", "blocks"))
    parser.add_argument("--roles-only", action="store_true")
    parser.add_argument(
        "--migrations-only",
        action="store_true",
        help="skip role bootstrap (roles are the orchestrator's; a seat has no CREATEROLE)",
    )
    args = parser.parse_args()

    if not args.migrations_only:
        if not args.admin_dsn or not args.admin_dsn.strip():
            print("BLOCKS_ADMIN_DSN (or --admin-dsn) is required", file=sys.stderr)
            return 2

    required = [("BLOCKS_DB_MIGRATOR_PASSWORD (or --migrator-password)", args.migrator_password)]
    if not args.migrations_only:
        required.extend([
            ("BLOCKS_DB_APP_PASSWORD (or --app-password)", args.app_password),
            ("BLOCKS_DB_DISPATCHER_PASSWORD (or --dispatcher-password)", args.dispatcher_password),
            ("BLOCKS_DB_BACKUP_PASSWORD (or --backup-password)", args.backup_password),
            ("BLOCKS_TEST_ADMIN_PASSWORD (or --test-admin-password)", args.test_admin_password),
        ])
    for name, value in required:
        if not value or not value.strip():
            print(f"{name} is required", file=sys.stderr)
            return 2

    if args.migrations_only:
        return _migrate(args)

    statements = render(
        ROLES_SQL.read_text(),
        {
            "migrator_pw": args.migrator_password,
            "app_pw": args.app_password,
            "dispatcher_pw": args.dispatcher_password,
            "backup_pw": args.backup_password,
            "test_admin_pw": args.test_admin_password,
            "db": args.database,
        },
    )
    try:
        with psycopg.connect(args.admin_dsn, autocommit=True) as conn:
            conn.execute(statements)
    except psycopg.errors.InsufficientPrivilege:
        print(
            "role bootstrap needs CREATEROLE, which no Blocks seat has.\n"
            "Roles are provisioned by the orchestrator from tools/bootstrap_roles.sql;\n"
            "use --migrations-only here, or run this file through psql as an admin.",
            file=sys.stderr,
        )
        return 2
    print(f"roles ready in database {args.database}")

    if args.roles_only:
        return 0
    return _migrate(args)


def _migrate(args) -> int:
    env = {
        **os.environ,
        "BLOCKS_DB_MIGRATOR_PASSWORD": args.migrator_password,
        "BLOCKS_DB_NAME": args.database,
    }
    return subprocess.call([sys.executable, str(ROOT / "tools" / "migrate.py")], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
