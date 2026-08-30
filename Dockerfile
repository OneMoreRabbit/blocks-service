# Immutable runtime image for the Blocks Service API process.
#
# NOT YET BUILT OR RUN. The development seat has no container runtime, so this
# file is authored against the deployment contract but unverified. See the
# `blocks-service-seat-capabilities` need in the Atlas vault; the first build
# must happen before the blocks-deployment contract moves off draft.

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
COPY contracts/ /app/contracts/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER blocks
EXPOSE 8080

# Migrations are NOT run on container start. They are a separate, ordered step
# the orchestrator invokes (`python tools/migrate.py`) with the migrator role;
# a replica set that each migrated itself on boot would race.
CMD ["uvicorn", "--factory", "blocks_service.app:create_app", \
     "--host", "0.0.0.0", "--port", "8080"]
