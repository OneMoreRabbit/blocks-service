#!/usr/bin/env python3
"""Audit block bindings for cross-tenant parents, BEFORE migration 0012.

Run as a deploy step immediately before applying `0012`, from the release image:

  python /app/tools/preflight_bindings.py --dsn "<blocks_backup DSN>"

**Read-only, and structurally so.** An earlier version lifted `FORCE ROW LEVEL
SECURITY` on three tables so the owner could see across tenants, and restored it
afterwards. That was wrong in a way worth naming: the lift is a committed schema
change, so a failure between the lift and the restore -- a dropped connection, a
killed process, an error in the audit itself -- leaves tenant isolation switched
off on a production database, and the safety check becomes the outage. A tool that
runs *because* something might be wrong must not be able to make things worse.

So it does not lift anything. It connects with an explicitly RLS-exempt read
credential -- Blocks already has `blocks_backup` for exactly this: `BYPASSRLS`,
`pg_read_all_data`, and no write grant of any kind -- and it **verifies that
exemption before trusting a clean result**. Without it the audit would see nothing
and report success, which is the most dangerous possible answer: "no findings"
from a connection that cannot see findings. That case fails closed.

The session is additionally pinned read-only, so even a mistake in this file
cannot write.

Exit codes: 0 clean, 1 findings, 2 could not audit (also a stop).
"""

from __future__ import annotations

import argparse
import sys

import psycopg
from psycopg.rows import dict_row

AUDIT = """
select
  b.id::text            as binding_id,
  b.tenant_id::text     as claimed_tenant,
  pb.tenant_id::text    as block_tenant,
  pr.tenant_id::text    as project_tenant
from blocks.block_bindings b
  left join blocks.physical_blocks pb on pb.id = b.block_id
  left join blocks.projects        pr on pr.id = b.project_id
where pb.tenant_id is distinct from b.tenant_id
   or pr.tenant_id is distinct from b.tenant_id
order by b.id
"""

CAPABILITY = """
select rolbypassrls, rolsuper, rolcanlogin
from pg_roles where rolname = current_user
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        required=True,
        help="An RLS-exempt READ credential (blocks_backup). Not the migrator, "
             "and not the owner: this audit must not be able to write.",
    )
    args = parser.parse_args()

    try:
        with psycopg.connect(args.dsn, row_factory=dict_row, autocommit=True) as conn:
            # Read-only for the whole session. Belt to the braces of using a role
            # with no write grants: two independent reasons this cannot mutate.
            conn.execute("set default_transaction_read_only = on")

            role = conn.execute(CAPABILITY).fetchone()
            if role is None or not (role["rolbypassrls"] or role["rolsuper"]):
                print(
                    "preflight: this credential is NOT exempt from row-level security, "
                    "so it cannot see bindings across tenants.\n"
                    "A clean result from it would be meaningless -- an audit that cannot "
                    "see a finding reporting none is the most dangerous possible answer.\n"
                    "Re-run with the blocks_backup role (BYPASSRLS, read-only).",
                    file=sys.stderr,
                )
                return 2

            findings = conn.execute(AUDIT).fetchall()
    except psycopg.Error as exc:
        print(f"preflight: could not audit bindings: {exc}", file=sys.stderr)
        return 2

    if not findings:
        print("preflight: 0 cross-tenant bindings. Safe to apply 0012.")
        return 0

    print(f"preflight: {len(findings)} cross-tenant binding(s). DO NOT APPLY 0012.")
    for row in findings:
        wrong = []
        if row["block_tenant"] != row["claimed_tenant"]:
            wrong.append(f"block in tenant {row['block_tenant']}")
        if row["project_tenant"] != row["claimed_tenant"]:
            wrong.append(f"project in tenant {row['project_tenant']}")
        print(f"  binding {row['binding_id']} claims tenant {row['claimed_tenant']}: "
              + ", ".join(wrong))
    print(
        "\nNothing has been changed; this tool cannot change anything.\n"
        "0012 would refuse these rows and roll back -- it remains the fail-closed\n"
        "authority if a write races this audit. Disposition is an architecture and\n"
        "operator decision: the binding is the record that a physical tag was pointed\n"
        "at a project, so it is not this tool's to delete, rewrite or re-home."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
