# Versioned resource APIs

Business endpoints use `/api/v1`. Authentication discovery, sign-in, sessions,
provider callbacks and platform information use that prefix too. Health and
readiness probes stay at `/healthz` and `/readyz`. Existing unversioned business
URLs are removed; configure the versioned callback URI with the identity provider.
Cookie names and root cookie paths remain unchanged.

The version is a wire-contract boundary, independent of the platform release.
Mount feature routers at the version root; do not embed the version in feature
modules. Operation identifiers remain stable domain/resource/action names.

## Resource conventions

`ResourceRouter` registers explicit `ResourceAction` handlers: list and create at
the collection path; retrieve, replace, update and delete at `/{id}`. Collections
use plural names without a trailing slash. GET returns 200, POST 201, PUT/PATCH
200 and DELETE an empty 204. PUT is full replacement; PATCH applies supplied
fields only. Services define those semantics; the router does not infer them.

Request DTOs reject unknown fields. Public response DTOs are separate from
persistence models. Collection endpoints bind `PageParams` explicitly and return
`Page[ResponseDTO]`: default limit 25, maximum 100, nonnegative offset and stable
ordering with a unique tie-breaker. Feature-specific filter/query DTOs must declare
an allowlist of filter and ordering fields; never accept arbitrary SQL names.

Every action requires a documented typed handler. The router assigns stable
operation IDs, common error schemas and default endpoint budgets: 120 reads or
30 writes per minute per transport peer, with explicit per-action overrides.
Endpoint limits run before cache hits and dependency resolution. Global and
authentication budgets retain their independent semantics. Limiter outages fail
closed; optional public-cache outages read through.

Authentication is required by default. Unsafe methods require the existing
session-bound CSRF header and origin checks. Public reads require explicit
`public_reads=True`; writes still require authentication. Resource services must
revalidate authorization and scope database queries before accessing data. Shared
infrastructure does not make arbitrary model fields publicly readable/writable.

Shared caching is disabled by default and rejected for protected actions. Only
explicitly public, identity-independent GET DTOs can opt in to the existing cache
policy. Mutations invalidate public caches after successful commit. Resource
services own async sessions, transactions, audit capture and conflict mapping.

## Delivery phases

Phase one establishes versioning, the resource action router, policy composition
and contract/security regressions using test-only handlers. Phase two adds the
application, category and release services, DTOs, filters and actions on the same
branch and pull request. No chart execution, secret generation or publication
state machine belongs to this API foundation.


## Application management endpoints

Applications, categories and releases have collection GET/POST and item
GET/PUT/PATCH/DELETE routes under the version root. Shared-definition reads require
a live session; writes additionally require installation-owner privileges.
Applications filter by slug/status, categories by slug, and releases by exact
application ID/source type. Search treats input as a literal substring. Ordering
accepts explicit fields only, always with an ID tie-breaker; chart versions sort
lexically, not as semantic versions.

Category associations use `/api/v1/applications/{application_id}/categories` for
GET/POST and the category ID as the final path component for DELETE. Associations
retain the actual composite key and do not acquire synthetic identifiers. Their
direct writes retain the model foundation's audit limitation; entity operations
are audited atomically, while configuration values are omitted from snapshots.

PUT replaces all writable state using the full input schema and its defaults;
PATCH changes only explicitly supplied fields and can clear a nullable icon.
Unknown input/query fields and unsupported filter/order choices return 422;
absent records return 404; uniqueness and protected-reference conflicts return
409 without exposing database details. Writes lock existing records and commit
with audit history in the same transaction. No public shared cache is enabled
for these authenticated endpoints, so no cache can bypass their access checks.

No release publication, cloning workflow, chart downloads/execution, runtime
configuration generation, secret storage or billing enforcement is included.
