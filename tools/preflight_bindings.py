#!/usr/bin/env python3
"""Audit block bindings for cross-tenant parents, BEFORE migration 0012.

Run this as a deploy step immediately before applying `0012`. It reports bindings
whose block or project belongs to a different tenant than the binding claims.

Such a row is possible in real history. API 0.4 wrote a binding without proving
either parent was visible in the caller's tenant, and `block_bindings` referenced
globally keyed parents, so a guessed or stale id from another tenant satisfied the
foreign key while the child row claimed the caller's tenant. `0012` adds the
tenant-composite keys that refuse that shape -- which means a database holding one
cannot be migrated until somebody decides what the row should have been.

**This tool never repairs anything.** It exits non-zero and stops the deploy. A
finding is an architecture and operator decision: the binding records that a
physical tag was pointed at a project, and deleting or re-homing it silently would
destroy the only evidence of what happened and could detach a tag a user is still
tapping. Disposition belongs to people who can ask which tenant was wronged.

  python tools/preflight_bindings.py --dsn "<migrator or owner dsn>"

Exit codes: 0 clean, 1 findings, 2 could not audit.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()

    try:
        with psycopg.connect(args.dsn, row_factory=dict_row, autocommit=True) as conn:
            # The audit must see every tenant's rows; that is the whole point, and
            # it is why this runs as a privileged deploy step rather than through
            # the application role. FORCE is lifted for the read and restored in
            # the same connection, so an interrupted audit cannot leave isolation
            # switched off for the migration that follows.
            conn.execute("set role blocks_owner")
            conn.execute("alter table blocks.block_bindings no force row level security")
            conn.execute("alter table blocks.physical_blocks no force row level security")
            conn.execute("alter table blocks.projects no force row level security")
            try:
                findings = conn.execute(AUDIT).fetchall()
            finally:
                conn.execute("alter table blocks.block_bindings force row level security")
                conn.execute("alter table blocks.physical_blocks force row level security")
                conn.execute("alter table blocks.projects force row level security")
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
        "\nNothing has been changed. 0012 would refuse these rows and roll back.\n"
        "Disposition is an architecture and operator decision: the binding is the\n"
        "record that a physical tag was pointed at a project, so it is not this\n"
        "tool's to delete, rewrite or re-home."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
