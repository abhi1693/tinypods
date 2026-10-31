# API contracts

## FastAPI guidance

- **Inputs:** Type every path/query/body parameter and return value. Prefer `Annotated[T, Query(...)]` and `Annotated[T, Path(...)]` for constraints and metadata; keep defaults on the Python parameter. Scalars such as UUIDs and integers do not need wrapper models. See [query validation](https://fastapi.tiangolo.com/tutorial/query-params-str-validations/) and [path validation](https://fastapi.tiangolo.com/tutorial/path-params-numeric-validations/).
- **Bodies:** Use Pydantic models for structured payloads and `Field` for field constraints. Use `Annotated[DTO, Body(...)]` when body metadata or embedding is needed. A route without a payload needs no request DTO or body. See [body fields](https://fastapi.tiangolo.com/tutorial/body-fields/).
- **Outputs:** Return annotated public DTOs and declare the intended response schema. Separate input and output models when their fields differ; response models validate and filter output. Returning a raw `Response` bypasses that validation. See [response models](https://fastapi.tiangolo.com/tutorial/response-model/).
- **Documentation and status:** Use route docstrings for OpenAPI descriptions. Document response meaning and status behavior; declare the default status on the decorator and additional responses explicitly. Response descriptions document behavior; they do not change runtime status codes. See [operation configuration](https://fastapi.tiangolo.com/tutorial/path-operation-configuration/).
- **Organization:** Group feature routes in `APIRouter` modules with shared prefixes, tags, responses and dependencies. Include them in the application factory. See [bigger applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/).
- **Dependencies:** Use typed `Annotated[T, Depends(provider)]` parameters for shared services and sessions. Pass the provider, not its result; reuse annotated aliases where useful. See [dependency injection](https://fastapi.tiangolo.com/tutorial/dependencies/).
- **I/O:** Use `async def` with awaited async clients. FastAPI runs synchronous route functions and dependencies in a thread pool, but ordinary synchronous helpers called inside an async route still run on the event loop. Explicitly offload unavoidable blocking I/O. See [async guidance](https://fastapi.tiangolo.com/async/).

## TinyPods choices

These are project rules, not requirements imposed by FastAPI.

- Keep public DTOs separate from SQLModel tables, settings and plugin APIs. Use distinct write/read DTOs when accepting input; reject unknown request fields through RequestDTO.
- Give each public JSON operation one HTTP method, an explicit response DTO and a unique camelCase operation ID. Keep IDs stable for generated clients. FastAPI also supports inferred return schemas and generated IDs; TinyPods deliberately requires explicit contracts. See [operation IDs](https://fastapi.tiangolo.com/advanced/path-operation-advanced-configuration/).
- Every route needs a meaningful docstring. The exporter checks public routes for a docstring and nonblank OpenAPI description. Document purpose, response semantics and material status behavior.
- Use PageParams (limit 1–100, default 25; offset >= 0) and Page[ResponseDTO] for collections. Use deterministic ordering with a unique tie-breaker. Declare query binding explicitly when using PageParams.
- Use OperationStatus for queued work and HTTP 202 when creating an operation. Use HTTP 201 for resource creation and 204 for a successful empty response where appropriate.
- Use ErrorResponse for errors. Its request_id matches the x-request-id header. Validation issues expose locations and codes, not input values or validator context. HTTP exceptions use standard status messages; unexpected failures use a fixed message. Existing logging is unchanged.
- Keep routes thin: inject services and sessions, await work, then return DTOs. Preserve transaction ownership in services and keep blocking database calls out of async request paths.

## Current coverage and remaining work

The only application routes are /healthz and /readyz. Both have typed responses and published docstrings; neither accepts a request body. Liveness returns 200. Readiness returns the same typed shape on 200 and 503 and awaits configured checks.

The exporter enforces operation IDs, public route documentation and primary response DTOs, including nested APIRouter routes. Router traversal uses FastAPI's public route-context helper, so the package requires FastAPI 0.162.0 or newer. CI checks deterministic export, schema validity and version alignment. It does not yet enforce every future input annotation, DTO separation, dependency boundary or async I/O choice automatically; those still require review and feature-specific tests.

Feature APIRouter modules, annotated input/dependency aliases and collection/operation endpoints should arrive with their features. The two probes remain in the factory; this PR does not add empty router packages or invent request bodies.

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
