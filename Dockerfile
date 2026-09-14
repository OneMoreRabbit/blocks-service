# Immutable runtime image for the Blocks Service API process.
#
# Built and run by the estate, not from this seat: no development seat carries a
# container runtime, by operator ruling, so the image is built by the orchestrator
# and runs in the stack as `blocks-api:8080`. The header used to say the image had
# never been built and that no deployment contract was active; both stopped being
# true some time ago.
#
# `blocks-deployment` **0.5 is the active contract** (accepted 2026-09-14). 0.4 is
# superseded as a contract and remains the estate's **current runtime description**
# -- it is what the running stack matches, which is why it is not merely history.
#
# Acceptance is not deployment: the estate's development-tagged image is unchanged
# until the separate cutover act, and nothing here authorises one.

FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim AS runtime

# Unprivileged, no shell, no home. The application never needs to write to its
# own filesystem: all state is in PostgreSQL.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin blocks

COPY --from=build /opt/venv /opt/venv
COPY migrations/ /app/migrations/
COPY tools/migrate.py /app/tools/migrate.py
# The deployment contract requires the binding preflight to run immediately
# before 0012. A required command absent from the artifact the estate deploys
# is not a deployable contract, so it ships from the same tagged source as the
# migrations it gates.
COPY tools/preflight_bindings.py /app/tools/preflight_bindings.py
COPY contracts/ /app/contracts/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER blocks
EXPOSE 8080

# One image, two processes, selected by command:
#
#   API    (default below)
#   worker  python -m blocks_service.dispatcher
#
# They share a domain model, a schema and a release cadence, so they ship
# together. They do NOT share a database role: the API runs as blocks_app and the
# worker as blocks_dispatcher, whose privileges are disjoint by design (ADR-0005).
#
# Migrations are NOT run on container start. They are a separate, ordered step the
# orchestrator invokes (`python tools/migrate.py`) with the migrator role; a
# replica set that each migrated itself on boot would race.
CMD ["uvicorn", "--factory", "blocks_service.app:create_app", \
     "--host", "0.0.0.0", "--port", "8080"]
