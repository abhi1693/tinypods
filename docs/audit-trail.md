# Audit trail

Core authentication writes automatically produce `AuditRecord` records in the
same PostgreSQL transaction. `ObjectType` identifies installed registered models
by a stable application/model label. Explicit migration execution synchronizes
the code registry idempotently and preserves types removed from installed code.
Historical targets use generic string IDs; their deletion does not erase history.
`changed_object` and `related_object` return references whose awaited
`resolve(session)` method uses only registered installed model classes. Resolution
returns no target for removed types or deleted objects and grants no authorization.

`AuditTrailMixin` supplies `created`, `last_updated`, `serialize_object()`,
`snapshot()` and `to_audit_record(action)`. It is abstract, not a physical table.
Register each concrete model with reviewed scalar allowlists and any explicitly
loaded relationships. Relationship snapshots contain identities, not recursive
object dumps. Timestamp-only updates and updates returning to their initial safe
state produce no history. Changes to excluded credential fields still produce an
update event, with equal safe before/after data and no secret values.

Authentication allowlists exclude password hashes, session/CSRF/state digests,
tokens, provider grants' encrypted material, OIDC subjects/session identifiers,
nonce/verifier values and callback destinations. Credential-keyed models receive
separate unique UUID identities for audit references; their original lookup keys
remain unchanged. Object representations contain only the registered type and
safe identity and are limited to 255 characters. Existing authentication records
receive migration-time timestamps; these are a baseline, not reconstructed history.
The installation identity's existing `created_at` remains unchanged.

## Transactions and execution context

Capture stages safe snapshots in PostgreSQL temporary storage, writing batches of
100 records. This is a batch size, not a transaction limit. The first prechange and
final postchange snapshots coalesce across flushes. Finalized records are inserted
with an INSERT/SELECT immediately before the outer commit. Creation followed by
deletion in one transaction produces no event. Savepoint rollback, outer rollback,
session close and failed history insertion roll back staged data with business
data. Finalized history never requires background tasks or a second commit.
Snapshot data and relationship identities are never truncated. Transactions still
consume ordinary database resources proportional to their work, and ORM sessions
retain their normal unit-of-work object references. Runtime roles need database
TEMP permission; transaction-pooling works because temporary tables are dropped
on commit. Do not share a session across concurrent tasks.

The API binds its server-generated request UUID and verified principal. CLI entry
and the current worker loop bind explicit `system:cli` / `system:worker` contexts.
Future job handlers should use a fresh `audit_context("system:worker", request_id=...)`
per job. Services can bind an authenticated user with `audit_context(name,
user_id=..., request_id=...)`. Missing explicit context is labeled
`system:unattributed`, with one correlation UUID per transaction. ContextVars are
isolated per asynchronous task and restored on scope exit. User names are retained
after deletion; the user FK uses SET NULL. System attribution uses the username
snapshot with an absent user FK, without separate actor-kind columns.

## Supported write boundary

Use runtime `Database` sessions and normal add/delete/flush. Session bulk DML and
legacy bulk persistence are rejected; auth cleanup/revocation uses locked ORM
operations instead. Audit identities are immutable. Load declared relationships
before mutating them; ORM-managed cascades are captured through the unit of work.
Database-side cascades, direct engine/connection SQL, textual SQL, external SQL
tools and independently constructed sessions' legacy bulk methods bypass capture;
they are privileged maintenance boundaries, not supported application writes.

PostgreSQL rejects updates/deletes of finalized history, allowing the user
FK's referential SET NULL operation and the explicit owner-only pruning path below. Application ORM writes to audit/type tables
are rejected; type synchronization is a controlled migration operation. These
controls do not constrain a database owner who can alter triggers or schemas.
Retention is operator-triggered only; no automatic expiry or scheduler is added. No public history API, catalog models,
outbox, metrics, SDK or deletion-protection framework is included.


## Explicit operator maintenance

These commands use the direct migration connection and default to dry-run reports:

```sh
uv run --no-sync tinypods audit cleanup-types
uv run --no-sync tinypods audit cleanup-types --apply
uv run --no-sync tinypods audit prune --older-than-days 90
uv run --no-sync tinypods audit prune --older-than-days 90 --apply --batch-size 500
uv run --no-sync tinypods audit prune --start 2026-01-01T00:00:00Z --end 2026-02-01T00:00:00Z --apply
```

`cleanup-types` imports and validates the intentional complete core registration
manifest. Missing/modified registration or import failure aborts cleanup; request
visibility and target-row existence never determine staleness. New core domains
must extend the intentional registration manifest. Types absent from that set are
listed. Apply deletes only those without `AuditRecord` references in either the
changed or related role. Referenced types are reported and retained. Row locks and
restrictive foreign keys protect concurrent writers; history never cascades away.

`prune` requires exactly one mode: a positive age, or both range endpoints. Age is
strictly older than the single execution-time UTC cutoff. Explicit timestamps
require an ISO 8601 offset or `Z`; naive/date-only values are rejected and offsets
normalize to UTC. Ranges are half-open `[start,end)`. Dry runs report the exact
window and matched count. Apply reports the actual deletion count, processes
bounded SQL batches in one atomic transaction and preserves nonmatched records
and all `ObjectType` rows. The batch size is not an overall history limit.

Pruning needs the history-table owner role (normally the migration role). A
transaction-local exception to the append-only trigger permits only DELETEs
inside the selected interval; UPDATEs and out-of-range deletes remain forbidden.
Runtime roles cannot enable this exception. Settings are cleared after successful
maintenance and cleared by PostgreSQL rollback on failure. A database owner can
already alter triggers; the guard does not claim to restrict that owner. No
operator database has been pruned as part of development or testing.

## Reproducing performance measurements

```sh
uv run --no-sync pytest tests/test_audit_performance.py -s --no-cov
```

This starts disposable PostgreSQL using the normal test fixture and writes
`reports/audit-performance.json`. It measures registration construction, loaded
10,000-member relationship serialization, and full 1,200-object insert/update
transactions with and without auditing. Measurements include safe before/after
reads, staging DDL/catalog checks, SQL batches and commit. Query-count assertions
guard against per-object SELECTs; elapsed times are observations, never gates.
All runs use Python allocation tracing, so times are diagnostic rather than a
production throughput promise. Tracing excludes PostgreSQL/server memory.

Registration construction and loaded serialization perform no database I/O.
Stable type UUIDs are cached per immutable registration. Safe original/post-SQL
reads are grouped by model in batches of 100, and staging existence is checked
once per flush plus finalization rather than once per batch. Relationship sorting
and copied snapshots still scale with member count; ORM unit-of-work references
and total database staging storage scale with the transaction. No count ceiling
or truncation is introduced. No architectural change is required for these
routine batching improvements; larger deployment-specific targets need their own
measurements before changing the design.
