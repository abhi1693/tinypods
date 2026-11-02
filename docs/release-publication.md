# Application and release lifecycle

Applications and releases each have one state field: `draft`, `in_review`, or
`published`. Their types are separate (`ApplicationState` and `ReleaseState`) so
they can evolve independently. The application maturity status remains
`experimental`, `stable`, or `premium` and has no publication/access meaning.

Owners can see and manage all states. Ordinary authenticated users see only
published applications, and only published, non-revoked releases whose parent
application is also published. Unpublishing an application hides all its releases
without changing their own states. Membership reads enforce the same application
visibility rule.

## API flow

1. Use application CRUD to create a draft and move its `state` through review or
   publication. Changing the application state does not change release states.
2. Create or edit a release with `state: draft` or `in_review`, an exact
   `chart_version_id`, curated `configuration_fields`, and declared architectures.
3. `POST /api/v1/releases/{id}/validation` returns only `valid` and safe `issues`.
   It verifies the chart and definition without persisting another representation.
4. `POST /api/v1/releases/{id}/publication` takes no body, revalidates, and returns
   the Release itself with `state: published` and `published_at`. It does not
   publish its parent application. Equivalent retries are idempotent.
5. `PATCH` the release state to `draft` or `in_review` to withdraw it from ordinary
   catalogue selection. Once published, its deployment fields remain frozen even
   when unpublished. Use `POST /api/v1/releases/{id}/clone` for an editable new
   revision; existing release IDs and chart pins remain unchanged.
6. `POST /api/v1/releases/{id}/deprecation` accepts `deprecated` and a nonblank
   `reason`; `/revocation` accepts a `reason` and permanently revokes the release.
   Notes and lifecycle metadata are mutable and audited. Revoked releases cannot
   be republished; historical records remain accessible to owners.

All operations require authentication. Writes require instance-owner privileges
and the current session CSRF header. Validation, cloning, publication and lifecycle
actions use the existing per-action rate limits. Invalid definitions expose codes
and paths, never rejected values. Public CRUD cannot bypass release validation by
setting its state directly to `published`.

## Storage and integrity

This feature adds **no tables**. It adds fields/constraints to existing applications
and releases and retains the existing change history. No publication snapshot,
canonical document, second publication digest, event table, cursor or delivery
infrastructure is created. Validation results are transient and are not stored in
a JSON column, file, or another helper table.

The release references the existing ChartVersion and its package digest. Every
validation/publication fetch verifies the exact pinned archive bytes. Published
release deployment fields and their first publication time are protected by a
PostgreSQL trigger, including SQL writes outside the API. Changing deployment
inputs requires a different draft revision. Source/configuration validation
happens without holding a database connection; the final state transition and
existing audit history commit atomically under the release row lock. The final
transaction rechecks the source under a shared repository lock held through
commit, so a disabled or changed repository cannot slip past validation. Chart
and ChartVersion identities retain their existing database protections. Concurrent
clones serialize revision assignment on the application row.

## Validation boundaries

Helm subcharts are contained within their parent Helm release, not separate
TinyPods applications. Only packaged/directory subcharts already present in the
verified parent archive are inspected. Missing dependencies are not downloaded.
Exact identities, ranges, aliases, cycles, duplicate versions and archive safety
are checked. Mutable latest references and prereleases are rejected. Conditions,
tags, globals, imports and baseline values are processed without executing chart
templates. Enabled chart schemas validate the resulting defaults. Curated field
paths, widget types, choices, required defaults and chart-default ownership are
validated independently; shared literal secrets remain forbidden.

JSON Schema drafts 4, 6, 7, 2019-09 and 2020-12 are supported. References must be
local and resolvable, including unused subschemas. Schema validation cannot fetch
URLs/files and runs in a bounded cancellable process with a ten-second deadline.
One process-local admission slot is reserved before archive download. Concurrent
validation/publication work receives HTTP 503 `validation_busy` without downloading
or queueing another archive; callers may retry. The ten-second deadline covers
CPU execution rather than time waiting for capacity. Cancellation and failures
release admission.

The bundle has cumulative archive/work limits, at most 64 physical/logical charts,
depth eight, 8,192 members and 64 MB expansion. Logical validation documents also
have an 8 MB work budget.

Architectures are an operator declaration, not proof of each container image's
platform support. The Helm renderer contract identifies the supported definition
format; optional inclusive core-version bounds must include the running version.
Cluster suitability, image inspection, rendering and deployment are separate
work. Publication here deploys nothing to Kubernetes. Catalogue reads remain
uncached and no background queue is introduced.

## Upstream request reuse

The application shares identical in-flight HTTP GETs only when URL, headers,
authentication policy and size bound match. At most 32 distinct reads are active
in this reader; additional distinct reads receive HTTP 503 `source_busy`. A
cancelled caller does not cancel another caller's read. The last caller removes
the response and cancels/joins unfinished work. Completed responses and validation
results are not cached. Every new operation still checks upstream availability;
Helm archive lookup constructs the validated YAML index only once.

Per-caller authentication and source rechecks remain mandatory. DNS pinning, TLS
hostname verification, redirect validation and credential isolation remain in the
transport. Connection pools and registry tokens are not reused across operations.
