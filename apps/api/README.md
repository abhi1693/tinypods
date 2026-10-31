# API contracts

## FastAPI guidance

- **Inputs:** Type every path/query/body parameter and return value. Prefer `Annotated[T, Query(...)]` and `Annotated[T, Path(...)]` for constraints and metadata; keep defaults on the Python parameter. Scalars such as UUIDs and integers do not need wrapper models. See [query validation](https://fastapi.tiangolo.com/tutorial/query-params-str-validations/) and [path validation](https://fastapi.tiangolo.com/tutorial/path-params-numeric-validations/).
- **Bodies:** Use Pydantic models for structured payloads and `Field` for field constraints. Use `Annotated[DTO, Body(...)]` when body metadata or embedding is needed. A route without a payload needs no request DTO or body. See [body fields](https://fastapi.tiangolo.com/tutorial/body-fields/).
- **Outputs:** Return annotated public DTOs and declare the intended response schema. Separate input and output models when their fields differ; response models validate and filter output. Returning a raw `Response` bypasses that validation. See [response models](https://fastapi.tiangolo.com/tutorial/response-model/).
- **Documentation and status:** Use route docstrings for OpenAPI descriptions. Keep docstrings focused on behavior and requirements. Put HTTP status codes, response descriptions, headers and callable request examples in route decorators and typed Body/Query/Header metadata. Response descriptions document behavior; they do not change runtime status codes. See [operation configuration](https://fastapi.tiangolo.com/tutorial/path-operation-configuration/).
- **Organization:** Group feature routes in `APIRouter` modules with shared prefixes, tags, responses and dependencies. Include them in the application factory. See [bigger applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/).
- **Dependencies:** Use typed `Annotated[T, Depends(provider)]` parameters for shared services and sessions. Pass the provider, not its result; reuse annotated aliases where useful. See [dependency injection](https://fastapi.tiangolo.com/tutorial/dependencies/).
- **I/O:** Use `async def` with awaited async clients. FastAPI runs synchronous route functions and dependencies in a thread pool, but ordinary synchronous helpers called inside an async route still run on the event loop. Explicitly offload unavoidable blocking I/O. See [async guidance](https://fastapi.tiangolo.com/async/).

## TinyPods choices

These are project rules, not requirements imposed by FastAPI.

- Keep public DTOs separate from SQLModel tables, settings and plugin APIs. Use distinct write/read DTOs when accepting input; reject unknown request fields through RequestDTO.
- Give each public operation one HTTP method and a unique operation ID built with `build_operation_id(domain, resource, action)`. IDs use lowerCamelCase in domain/resource/action order, for example `authSessionGet` and `authZitadelLoginStart`. Resource/action components are stable contract names rather than Python function names or file paths. Duplicate IDs and invalid casing fail schema export. JSON operations also declare an explicit response DTO. Keep IDs stable for generated clients. FastAPI also supports inferred return schemas and generated IDs; TinyPods deliberately requires explicit contracts. See [operation IDs](https://fastapi.tiangolo.com/advanced/path-operation-advanced-configuration/).
- Follow the shared [documentation guidance](../../CONTRIBUTING.md#documentation-and-comments). Every route needs a meaningful docstring. The exporter checks public routes for a docstring and nonblank OpenAPI description. Document purpose, response semantics and material status behavior.
- Use PageParams (limit 1–100, default 25; offset >= 0) and Page[ResponseDTO] for collections. Use deterministic ordering with a unique tie-breaker. Declare query binding explicitly when using PageParams.
- Use OperationStatus for queued work and HTTP 202 when creating an operation. Use HTTP 201 for resource creation and 204 for a successful empty response where appropriate.
- Use ErrorResponse for errors. Its request_id matches the x-request-id header. Validation issues expose locations and codes, not input values or validator context. HTTP exceptions use standard status messages; unexpected failures use a fixed message. Existing logging is unchanged.
- Keep routes thin: inject services and sessions, await work, then return DTOs. Preserve transaction ownership in services and keep blocking database calls out of async request paths.

## Current coverage and remaining work

The application exposes /healthz, /readyz, public /info and the local /auth routes. All have typed responses and published docstrings; login accepts a JSON body. Liveness returns 200. Readiness returns the same typed shape on 200 and 503 and awaits configured checks.

The exporter enforces operation IDs, public route documentation and primary response DTOs, including nested APIRouter routes. Router traversal uses FastAPI's public route-context helper, so the package requires FastAPI 0.162.0 or newer. CI checks deterministic export, schema validity and version alignment. It does not yet enforce every future input annotation, DTO separation, dependency boundary or async I/O choice automatically; those still require review and feature-specific tests.

Feature APIRouter modules, annotated input/dependency aliases and collection/operation endpoints should arrive with their features. The application factory owns the probes; feature routers own their operations.

## Export

After changing public API routes, DTOs, parameter constraints, response schemas or descriptions, regenerate the contract from the workspace root before committing:

```sh
uv run tinypods-openapi
uv run tinypods-openapi --check
uv run openapi-spec-validator openapi.json
```

The exporter uses explicit built-in defaults without loading deployment settings, starting lifespan or connecting to PostgreSQL. It rejects missing or duplicate operation IDs and missing response models. The checked-in openapi.json is the core client input for future Orval generation. Regenerate it when routes change; CI rejects drift and invalid OpenAPI. Plugin schemas are exported separately by their own packages.

The existing CI workflow runs on every pull request without path filters. It runs `tinypods-openapi --check`, which builds a fresh application schema (including nested routers) and compares it with the checked-in JSON, then validates the document. A stale or missing file fails CI. Internal implementation changes that leave the public schema unchanged need no artificial JSON diff.

## Project version

VERSION at the workspace root is the only maintained project version. After changing it, run:

```sh
python scripts/sync_version.py
uv lock
uv sync --locked --all-packages --no-editable
uv run tinypods-openapi
```

The sync script generates matching versions in every workspace pyproject.toml. CI rejects drift. These generated metadata values let each setuptools package build on its own without reaching outside its directory. Runtime code reads the installed tinypods-core distribution version; the API and OpenAPI export use that same value. No source-checkout file is required at runtime.


## Public response cache

GET /info is the initial opt-in metadata endpoint. Installing CachedReadRoute does not enable caching automatically. Only a GET endpoint with an explicit CachePolicy opts in. Current health/readiness and ordinary routes have no policy and run live; there is no special path-name blacklist. A future public route can opt in with CachedReadRoute and CachePolicy only if its response is independent of identity, cookies, authorization and varying request headers. Never opt authenticated or personalized routes into the shared cache.

The route wrapper runs before dependencies, so a valid hit avoids database checkout. Redis operations use the native async client with 200 ms socket/connect timeouts, no retries and a five-second failure circuit breaker. A miss may wait up to one second for another loader. Cache outages and write failures read through to the source.

Keys include platform version, hashed database identity, namespace, policy revision, path and canonical query parameters. Repeated query-value order is preserved. Requests with undeclared query parameters bypass the cache, so ignored query variations do not allocate entries. Only bounded HTTP 200 JSON DTOs without cookies, custom headers, Vary or cache-control directives are stored. Hits are validated against the response DTO and receive a fresh request ID from the outer logging middleware. Browsers/proxies receive Cache-Control: no-store; X-Cache reports HIT, MISS or BYPASS.

TINYPODS_CACHE_ENABLED disables caching. TTL settings default to 300 seconds for content and 600 for metadata, bounded to 1–3600 seconds. TINYPODS_CACHE_MAX_BYTES defaults to 1 MB. TINYPODS_CACHE_NAMESPACE separates operator environments. TINYPODS_CACHE_VALKEY_URL can select a separate endpoint; otherwise the existing private Valkey URL is used.

Await ResponseCache.invalidate only after a successful committed source change. Generation rotation and owner-checked publication prevent an earlier fill from repopulating an invalidated generation. The cache never flushes the database or deletes queue keys. There are no catalog writes to hook yet; those services must wire post-commit invalidation when implemented. TTL remains the fallback freshness bound after failed invalidation; strict revocation-sensitive data must not use this cache.

The default Compose service keeps its AOF and noeviction policy, stays on the private data network and runs non-root with dropped capabilities. Cache and queue still share capacity: use the separate cache URL when resource policies need to differ. Cached data is disposable, not a source of truth. In-process public cache policy is a developer contract, not an authorization sandbox.


Cache storage errors are handled at the narrow Redis/socket boundary so optional I/O can fail open. Invalid cached JSON/DTO values are handled separately as data errors. Endpoint exceptions and cancellation are not swallowed. Publication and lock cleanup suppress only CacheUnavailable; cleanup is awaited under a cancellation shield with the same bounded socket deadlines. Pool shutdown is awaited through the application lifespan even if another resource's cleanup fails.

JSON identity construction, hashing and DTO serialization remain bounded synchronous CPU work. They do not perform network I/O. Cache methods, including invalidation and close, must be awaited; they are not thread-pool adapters.


## Authentication and service authorization

See [authentication setup and security](../../docs/authentication.md) for local and
Zitadel configuration, public discovery, server-side OIDC transactions, identity
admission/linking, logout semantics and precise integration evidence.

`PrincipalDependency` depends on provider-independent sessions and checks bound
CSRF on every method except GET/HEAD/OPTIONS. Resource services must call
`AuthorizationService.require_access` with the resource's stored owner ID and
`Scope.APP`, `CLUSTER` or `SECRET`; catalog administration uses `CATALOG_ADMIN`.
`SessionService.require_access` delegates to that same policy. There are no app,
cluster, secret or catalog CRUD services yet; resource services must invoke this
boundary before data access. Even an installation owner cannot cross another
owner's resource scope. Unknown scopes, absent owners and stale/forged principals
fail closed. Policy uses an injected `PrincipalValidator` and opaque credential
context, so non-browser authenticators can validate credentials without password/cookie assumptions.
API keys themselves are deferred.

The callback is an intentional HTTP 303 redirect with a documented Location
header and no JSON response body. The exporter accepts this explicit redirect
contract alongside ordinary typed JSON responses and HTTP 204 operations.

## Endpoint rate policies

Use `RateLimitedRoute` on feature APIRouters (the application root and auth router
already do). It composes around `CachedReadRoute` so reservations run before cache
hits, body validation and dependency execution:

```python
router = APIRouter(route_class=RateLimitedRoute)

@router.post(
    "/uploads", operation_id="createUpload", response_model=UploadResponse,
    openapi_extra=EndpointRatePolicy(name="uploads", limit=20).openapi_extra(),
)
async def upload(...) -> UploadResponse:
    ...
```

Policies default to a 60,000 ms window and use per-peer subjects. Merge
`openapi_extra` dictionaries to combine rate and public-cache policies. Use a
stable unique name per budget; deliberately reusing a name/configuration shares
its budget. For non-HTTP jobs or user/account quotas, inject the common core
limiter and call `await limiter.reserve(RatePolicy(...), subject)`; interpret the
returned decision and never turn `RateLimitUnavailable` into an allowance.
The optional global middleware budget includes unmatched paths and all HTTP
methods, regardless of route class. It excludes `/healthz` and `/readyz` so probes
remain useful during saturation. Endpoint and auth limits cannot be disabled by
setting the global budget to zero. Pools close with the API lifespan even when
another cleanup operation fails. The cache retains its independent fail-open
semantics for eligible public data; rate limiting fails closed.
