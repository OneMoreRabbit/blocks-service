#!/usr/bin/env python3
"""Stand up a Blocks database on a development seat.

Applies tools/bootstrap_roles.sql -- the same file an operator would run through
psql -- and then the migrations. It exists so a seat with no psql client can
still reach a byte-identical database, and so bootstrap_roles.sql has exactly one
home rather than a Python paraphrase that can drift from it.

Requires an administrative DSN (BLOCKS_ADMIN_DSN). Development only: it never
runs against, and never needs to run against, a deployed environment, where the
orchestrator owns role provisioning.
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
        help="superuser DSN; defaults to $BLOCKS_ADMIN_DSN",
    )
    parser.add_argument("--migrator-password", default=os.environ.get("BLOCKS_DB_MIGRATOR_PASSWORD", "blocks-dev-migrator"))
    parser.add_argument("--app-password", default=os.environ.get("BLOCKS_DB_APP_PASSWORD", "blocks-dev-app"))
    parser.add_argument("--database", default=os.environ.get("BLOCKS_DB_NAME", "blocks"))
    parser.add_argument("--roles-only", action="store_true")
    args = parser.parse_args()

    if not args.admin_dsn:
        print("BLOCKS_ADMIN_DSN (or --admin-dsn) is required", file=sys.stderr)
        return 2

    statements = render(
        ROLES_SQL.read_text(),
        {
            "migrator_pw": args.migrator_password,
            "app_pw": args.app_password,
            "db": args.database,
        },
    )
    with psycopg.connect(args.admin_dsn, autocommit=True) as conn:
        conn.execute(statements)
    print(f"roles ready in database {args.database}")

    if args.roles_only:
        return 0

    env = {
        **os.environ,
        "BLOCKS_DB_MIGRATOR_PASSWORD": args.migrator_password,
        "BLOCKS_DB_NAME": args.database,
    }
    return subprocess.call([sys.executable, str(ROOT / "tools" / "migrate.py")], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
