# Blocks Service

The authoritative domain, data, synchronization, and device-coordination service
for Blocks. One component: API process, background worker, schema migrations, and
outbox dispatcher share a domain model, a schema, and a release cadence.

**Architecture, contracts, plans, and decisions live in the Atlas-Blocks vault**,
not in this repository — see `AGENTS.md`. Run `sh scripts/atlas-context.sh` for
the current briefing.

## Layout

| Path | What |
|---|---|
| `src/blocks_service/` | The service. `db.py` and `migrations/` are where tenant isolation actually lives. |
| `migrations/` | Forward-only SQL, applied by `tools/migrate.py`. |
| `contracts/events/` | Hand-authored event JSON Schemas — the published contract, validated against in tests. |
| `tools/` | Migration runner, dev database bootstrap, contract export. |
| `tests/` | Run against real PostgreSQL as the real unprivileged runtime role. |

## Local setup

Requires the seat's platform PostgreSQL container (`postgres:5432`).

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env
export BLOCKS_ADMIN_DSN="host=postgres port=5432 user=blocks password=blocks-dev dbname=blocks"
.venv/bin/python tools/dev_db.py      # roles, then migrations
.venv/bin/python -m pytest
```

Run it:

```sh
.venv/bin/python -m uvicorn --app-dir src --factory blocks_service.app:create_app --port 8080
```

`GET /health/ready` reports `tenant_isolation: enforced` only if the connected
role can neither bypass RLS nor own the tables.

## Backup and restore

`tools/restore_drill.py` dumps the database, restores it into a scratch copy, and
verifies that **row-level security survived the round trip** — not just the rows.
A restore that returns every row while silently losing RLS looks like a complete
success and is a total tenant-isolation failure.

```sh
.venv/bin/python tools/restore_drill.py --source-db blocks --scratch-db blocks_drill
```

It needs `pg_dump` and `pg_restore` on `PATH`, which are **not yet part of the
seat image** (`sudo apt-get install postgresql-client`). The drill's tests skip
with a reason when they are missing.

## The one thing to know before changing anything

Tenant isolation is enforced by PostgreSQL, not by this code. Every tenant-scoped
table has `FORCE ROW LEVEL SECURITY` and a policy comparing `tenant_id` against
a transaction-local setting; the service connects as `blocks_app`, which is
neither an owner nor `BYPASSRLS`. Application queries therefore carry no tenant
predicate — the absence of `where tenant_id = ...` in `repository.py` is the
design, not an oversight.

Two consequences:

- Reach tenant data only through `Database.tenant_transaction`. An unscoped
  connection sees zero rows, so a mistake fails closed and loudly.
- Never grant the runtime role ownership, `SUPERUSER`, or `BYPASSRLS`. Any of
  the three turns every policy in the schema into a comment.

## Not yet built

The container image is unbuilt (no container runtime on the seat), there is no
outbox dispatcher or worker process, and identity runs against a substituted
verifier rather than a real OIDC issuer. See the development plan in the vault.
