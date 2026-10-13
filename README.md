# Blocks Service

Blocks helps people keep projects, notes, links, and parked work in sync across
devices. This repository contains the Service API, its PostgreSQL schema and
migrations, and a background worker that dispatches change events. The Android
app can work locally and sync with Service when connected. A Web client is
planned; it is not a live product.

Service authenticates each request, maps the verified identity to a tenant,
and uses PostgreSQL row-level security to keep tenants separate. Its API covers
sessions and identity bootstrap, projects and their order, notes and links,
parking, blocks and map operations, and a change feed for device sync. The
running API describes its routes at `/docs` and `/openapi.json`.

## Run locally

You need Python 3.12 or newer, a PostgreSQL server, and a database administrator
who can create the `blocks` database and provision roles. This repository does
not start PostgreSQL or create cluster roles for you. Provision the local roles
from [`tools/bootstrap_roles.sql`](tools/bootstrap_roles.sql) before migrating;
the script expects caller-supplied `migrator_pw`, `app_pw`, `dispatcher_pw`,
`backup_pw`, `test_admin_pw`, and `db` psql variables. Use passwords you choose
for this local database. The test administrator must have `CREATEDB`; the API
role must not own tables or bypass row-level security.

From the repository root, install the application and development dependencies,
then edit `.env` to match your local database and chosen passwords:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

The example uses `localhost:5432` and the database name `blocks`. Set
`BLOCKS_DB_APP_PASSWORD`, `BLOCKS_DB_MIGRATOR_PASSWORD`, and
`BLOCKS_DB_DISPATCHER_PASSWORD` in your untracked `.env` to the passwords you
provisioned. The example values are placeholders and will not match a real
database. Keep `.env` private.

Apply the forward-only migrations with the migrator role, then start the API:

```sh
.venv/bin/python tools/migrate.py
.venv/bin/python -m uvicorn --app-dir src --factory blocks_service.app:create_app --port 8080
```

In another terminal, from the repository root, start the outbox worker:

```sh
PYTHONPATH=src .venv/bin/python -m blocks_service.dispatcher
```

The API and worker read `.env` from the repository root. Check
`http://localhost:8080/health/ready` for readiness. Business routes require a
Bearer identity token; the example `BLOCKS_IDENTITY_MODE=dev` is for local
development and tests only. A deployed Service must use a configured OIDC
issuer. The worker currently logs dispatched event envelopes.

## Run tests

The suite uses a real PostgreSQL database, creates and drops
`blocks_service_test`, and connects as the unprivileged roles it tests. It does
not use an in-memory database. Provision the development-only
`blocks_test_admin` role from `tools/bootstrap_roles.sql` and supply its local
DSN. Export the values in `.env` because the test harness reads the app and
migrator passwords from process environment variables:

```sh
set -a
. ./.env
set +a
export BLOCKS_TEST_ADMIN_DSN='host=localhost port=5432 dbname=blocks user=blocks_test_admin password=<your-local-test-admin-password>'
.venv/bin/python -m pytest
```

Use an isolated local PostgreSQL instance for tests: the suite replaces the
named test database. Do not point these commands at an existing shared or
production database.

## Repository map

| Path | Contents |
|---|---|
| `src/blocks_service/` | API, domain model, database access, and worker |
| `migrations/` | Forward-only PostgreSQL migrations |
| `contracts/events/` | Event JSON Schemas |
| `tools/` | Role-bootstrap SQL, migration runner, and contract tools |
| `tests/` | API and database tests |

## License

No license has been supplied: this repository currently has no `LICENSE` or
`COPYING` file. The owner still needs to choose and add a license; public
visibility alone does not answer reuse terms.
