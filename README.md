# TinyPods

Manage complete applications on an existing Kubernetes cluster.

## Backend development

Install Python 3.13 and uv, then run from the repository root:

```sh
uv sync --locked --all-packages --no-editable
uv run --no-sync tinypods-api
uv run --no-sync tinypods doctor
uv run --no-sync tinypods-worker --check
```

The API listens on `0.0.0.0:8000` by default for LAN access. Set
`TINYPODS_API_HOST` and `TINYPODS_API_PORT` to override the direct process binding. `/healthz` reports process liveness.
`/readyz` returns 503 outside the application lifespan or when a registered
dependency fails or times out. If a database URL is configured, readiness verifies
the migrated installation record. Liveness does not depend on the database.

Settings use the `TINYPODS_` environment prefix; `.env.example` lists supported
settings. Environment files are not loaded implicitly. Keep credentials out of
commits. Valkey configuration is reserved for the queue integration work.

## PostgreSQL

The Typer CLI provides typed commands and `--help` at each command level.
Provide a PostgreSQL database and export `TINYPODS_DATABASE_URL` before running:

```sh
uv run --no-sync tinypods db upgrade
uv run --no-sync tinypods db current
uv run --no-sync tinypods db check
uv run --no-sync tinypods doctor --database
```

Run these commands from the repository root, or pass `--config` with the Alembic
configuration path. Migrations run explicitly, never during API or worker startup.
The initial migration creates a persistent installation identity. Core models own
separate metadata; core migration discovery excludes plugin and unmanaged tables.
New core schema changes get their own Alembic revision.

Application sessions use SQLModel with SQLAlchemy's async engine and psycopg.
Each request or job owns a short-lived session. `Database.transaction()` commits on
success and rolls back on failure; `Database.session()` rolls back uncommitted
changes on exit. Services must explicitly commit writes made in request sessions.
Do not share a session across concurrent tasks. Persistence models and response
schemas stay separate. Database queries, cleanup and readiness checks are awaited.
Alembic uses synchronous connections only in the explicit administration command.

Direct connections use a bounded application pool. For PgBouncer transaction
pooling, set `TINYPODS_DATABASE_MODE=pgbouncer` and point the runtime URL at the
pooler. This uses `NullPool` and disables prepared statements for compatibility
with poolers that do not track them. Also set `TINYPODS_MIGRATION_DATABASE_URL`
to the direct PostgreSQL endpoint: migrations need a stable session for their
advisory lock. With direct mode, this override is optional. The credentials may
be identical; migrations need schema-change permissions, not superuser access.
Separate runtime and migration roles can be used when deploying with restricted
runtime permissions. The migration lock prevents concurrent core migrators.

The worker starts an idle, signal-aware process with a heartbeat. Queue dispatch
and operation handlers are separate issues. Plugin SDK, runtime and authentication packages will be added with their own
implementation issues rather than as placeholder packages.

## Checks

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest
```

Tests require a running Docker daemon. They start pinned PostgreSQL and
PgBouncer images on a private network with random test credentials and ephemeral
localhost ports, then remove only those test containers. No existing database is
used. Tests cover installed commands, HTTP lifecycle, async request sessions,
transaction rollback, populated upgrades, failed migration rollback, migration
locking, plugin-table preservation, redaction and event-loop responsiveness. Pytest enforces
90% branch-aware coverage across the implemented backend packages and writes
JUnit and coverage XML into `reports/`. CI uses the pinned shared Python workflow
from `abhi1693/actions` and uploads those reports. Strict mypy checks include all
backend source packages; generated migration files are excluded.

Runnable entry points live under `apps/`; shared Python packages live under
`packages/`. Each package maps its import namespace to its flat `src/` directory.
Tests live at the repository root. Frontend work follows the tracker dependencies.


## Docker Compose

Use Docker Engine with Compose 2.24.4 or later. This follows the base, build and
worker override layout used in DevFeed:

```sh
cp compose.env.example .env
# Set POSTGRES_PASSWORD and VALKEY_PASSWORD to different random hex strings.
docker compose -f compose.yaml -f compose.build.yaml -f compose.workers.yaml build
docker compose -f compose.yaml -f compose.build.yaml -f compose.workers.yaml up -d --wait
```

The example enables the `local` profile for PostgreSQL and Valkey. The one-shot
`migrate` service applies core migrations before the API and worker start. The
backend image runs as an unprivileged user with a read-only filesystem and a
temporary `/tmp`. It includes the API, worker and migration CLI; no web service
is started yet. All upstream image references pin ARM64/AMD64 manifest digests.

The API is available at `http://<machine-LAN-IP>:8000`. Set
`TINYPODS_PUBLISH_HOST` and `TINYPODS_PUBLISH_PORT` in `.env` to change the
published interface and port; the host defaults to `0.0.0.0` (all interfaces).
Use `127.0.0.1` for host-only access. Inside the container, the API binds to
`0.0.0.0:8000`; the published host address belongs to the Docker host.

Database and queue ports are not published. Both use the private `data` network
and named volumes. Queue Valkey uses AOF with a one-second fsync interval,
128 MiB of data memory and `noeviction`; it rejects writes when full rather than
evicting queued work. The container has a separate 384 MiB memory limit. An
evictable cache must use a different instance. PostgreSQL/Valkey health checks
gate startup, and API readiness checks the migrated database.

The worker currently logs its heartbeat but has no queue handlers. Compose tracks
its running state; a queue-backed worker health probe belongs with queue runtime
support. No heartbeat files or file-based health probes are used. The worker
override enables workers and configures replicas, CPU and memory independently.
Omit it for an API-only setup, or enable the `workers` profile explicitly.

```sh
docker compose -f compose.yaml -f compose.workers.yaml logs -f api worker
docker compose -f compose.yaml -f compose.workers.yaml run --rm migrate
docker compose -f compose.yaml -f compose.workers.yaml down
```

`down` preserves data. Only use `down --volumes` when intentionally deleting the
local databases. Omit the build override and set `TINYPODS_BACKEND_IMAGE` when
using an already-built image.

For external PostgreSQL/Valkey, clear `COMPOSE_PROFILES` in `.env` and replace
`TINYPODS_DATABASE_URL` and `TINYPODS_VALKEY_URL` with reachable service URLs.
No local database/queue containers are started in that mode. For PgBouncer, set
`TINYPODS_DATABASE_MODE=pgbouncer` and supply the direct migration URL. Configure
TLS in the service URLs as required by the external provider. Container
`localhost` addresses the container itself, so use a reachable DNS name or IP.

The test suite builds this image, starts isolated Compose projects, recreates
containers to verify database/queue persistence, and runs against externally
provided services. It also verifies private networking, worker overrides, and
host/port configuration. ARM64 image inputs are verified; runtime integration
tests execute on the host architecture.


### Watch local changes

Use the build and worker overrides with `up --watch` to rebuild and recreate
the backend containers when source, dependencies or migrations change:

```sh
docker compose -f compose.yaml -f compose.build.yaml -f compose.workers.yaml up --watch
```

API, worker and migration services use separate local image tags, sharing the
same Dockerfile and cached build layers. The migration job stays separate from
the long-running application processes. Image-based deployment without the build
override does not enable source watching.

### Optional local image registry

Set `TINYPODS_IMAGE_REGISTRY=registry.home` in `.env` to use a pull-through
registry. Leave it empty to use the original upstream registries. The path layout
matches DevFeed: Docker Hub library images use
`<registry>/docker.io/library/...`, Valkey uses `<registry>/docker.io/valkey/...`,
and uv uses `<registry>/ghcr.io/astral-sh/...`. This covers runtime dependencies
and Dockerfile build inputs while keeping immutable image digests.

The Docker daemon and builder must trust the registry's certificate, whose names
must include the configured hostname. Local backend images are built locally;
the registry prefix does not redirect them. `TINYPODS_BACKEND_IMAGE` still selects
an existing backend image when the build override is omitted.
