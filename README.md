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
dependency check fails or times out. Until dependency adapters are added,
readiness checks the API runtime only; it does not claim database or queue connectivity.

Settings use the `TINYPODS_` environment prefix; `.env.example` lists the initial
non-secret settings. Environment files are not loaded implicitly. Optional
`TINYPODS_DATABASE_URL` and `TINYPODS_VALKEY_URL` are secret values reserved for
the database and queue integration work. They do not open connections yet.

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

Tests cover installed commands, a real HTTP server, shutdown, configuration
validation, log redaction and dependency failure/timeout behavior. Pytest enforces
90% branch-aware coverage across the implemented backend packages and writes
JUnit and coverage XML into `reports/`. CI uses the pinned shared Python workflow
from `abhi1693/actions` and uploads those reports.

Runnable entry points live under `apps/`; shared Python packages live under
`packages/`. Each package maps its import namespace to its flat `src/` directory.
Tests live at the repository root. Frontend, persistence and Compose work follow
the tracker dependencies.
