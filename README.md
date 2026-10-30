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

The API listens on `127.0.0.1:8000`. `/healthz` reports process liveness.
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
Tests live at the repository root. Frontend and Compose work follow
the tracker dependencies.
