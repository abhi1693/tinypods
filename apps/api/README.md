# API contracts

Use explicit Pydantic request and response DTOs from the HTTP layer. Do not expose SQLModel tables, settings, credentials or plugin APIs through the core schema. Request DTOs reject unknown fields.

Give every route a meaningful docstring explaining its purpose, response semantics and material HTTP status behavior. FastAPI publishes the public docstring in OpenAPI; the exporter rejects missing or blank public documentation.

Give each public route one method and a unique, explicit camelCase operation ID. Treat operation IDs and response fields as client compatibility commitments. Declare response models for successful and alternate responses; use 204 for responses without a body.

Collection routes use PageParams (limit 1–100, default 25; offset >= 0) and Page[ResponseDTO]. Order results deterministically with a unique tie-breaker. OperationStatus describes queued work; routes that create operations should return 202. This issue adds contracts, not collection or operation endpoints.

Errors use ErrorResponse with code, message, request_id and validation issues. Request IDs match the x-request-id response header. Validation issues carry locations and codes, excluding input values and validator context. HTTP exceptions use standard status messages; unexpected failures use a fixed message. Exception details remain in existing logging. No logging redaction is added.

Liveness and readiness keep their existing response shapes. Readiness returns the same typed body on 200 and 503.

## Export

Run from the workspace root:

```sh
uv run tinypods-openapi
uv run tinypods-openapi --check
uv run openapi-spec-validator openapi.json
```

The exporter uses explicit built-in defaults without loading deployment settings, starting lifespan or connecting to PostgreSQL. It rejects missing or duplicate operation IDs and missing response models. The checked-in openapi.json is the core client input for future Orval generation. Regenerate it when routes change; CI rejects drift and invalid OpenAPI. Plugin schemas are exported separately by their own packages.

## Project version

VERSION at the workspace root is the only maintained project version. After changing it, run:

```sh
python scripts/sync_version.py
uv lock
uv sync --locked --all-packages --no-editable
uv run tinypods-openapi
```

The sync script generates matching versions in every workspace pyproject.toml. CI rejects drift. These generated metadata values let each setuptools package build on its own without reaching outside its directory. Runtime code reads the installed tinypods-core distribution version; the API and OpenAPI export use that same value. No source-checkout file is required at runtime.
