# Transactional outbox persistence

The core outbox stores delivery intent in PostgreSQL alongside a domain change.
It has no operation foreign key, transport dependency, session hook, public API
or production event producer. Operations can become a producer later. Tests use
independent catalog and identity schemas to demonstrate reuse without changing
catalog publication.

## Producing events

A domain owns a Pydantic schema containing only safe, necessary delivery metadata.
The event name is a code-owned class variable:

~~~python
from typing import ClassVar
from uuid import UUID

from tinypods_core.outbox import EventPayload, append_event


class OperationAccepted(EventPayload):
    event_type: ClassVar[str] = "operations.accepted"
    operation_id: UUID


# Illustrative future service: operation persistence is not implemented here.
async with database.transaction() as session:
    operation = await create_authorized_operation(session, principal, request)
    append_event(session, OperationAccepted(operation_id=operation.id))
# Respond only after successful commit.
~~~

The helper validates and snapshots the schema, then adds an event to the supplied
session. It does no I/O and does not flush, commit, roll back, open or close a
session. Business validation, authorization and idempotency remain the domain's
responsibility. Repeated calls deliberately create distinct event IDs; business
request idempotency must decide whether another event is warranted. Do not catch
an event persistence failure and commit the business change separately.

Extra fields are forbidden. Event names are dotted lowercase identifiers, at
most 128 characters; each segment starts with a letter and may contain digits or
underscores. Schemas must serialize
to finite JSON objects. The helper checks the UTF-8 encoded JSON against 16 KiB;
PostgreSQL independently enforces **16,384 bytes of JSONB text**, including spaces.
Normalization, particularly exponent expansion, can make the database reject a
payload that passed the preliminary check. That failure rolls back the entire
business transaction. Keep events small and prefer stable IDs.

The payload is omitted from model repr. There are no new logs or response DTOs.
This is not a secret detector: producers must never include credentials,
configuration dumps, request bodies or raw exceptions. No arbitrary external
event submission is exposed. The event type identifies its typed payload contract. A breaking payload change
requires a distinct event type; keep the old consumer compatible until pending
events of that type have drained. No premature version suffix is required.

## Storage and publication

The five columns are id (UUID primary key), event_type (varchar 128),
payload (JSONB object), created (timestamptz), and
published_at (nullable timestamptz). There is no duplicate update timestamp.
The partial index on (created, id) where published_at is null supports bounded,
deterministic oldest-first pending reads. This ordering is a scan order, not a
cross-transaction commit or delivery ordering guarantee.

New events must be pending. Database constraints and a trigger freeze the
identity, type, payload and creation time after insertion. The sole
allowed transition is null to a publication acknowledgment time at or after
created; a repeated identical acknowledgment is harmless. Published timestamps
cannot be changed or cleared, and pending rows cannot be deleted. These guards
protect normal application SQL, not privileged administrators disabling triggers.

pending_events reads at most 1,000 rows, defaults to 100, and neither claims nor
publishes them. acknowledge_event conditionally updates a still-pending identity
in the caller's transaction. It returns false for an unknown/already acknowledged
event, preserves the original time, and requires a timezone-aware timestamp.

The future dispatcher must read in a short session, publish outside database
transactions, wait for transport acknowledgment, and then acknowledge in another
short transaction. Never acknowledge before successful publication. A crash
between transport acknowledgment and database acknowledgment can publish the
same event again. Delivery is therefore at least once: transport and consumers
must carry the stable event ID and make effects idempotent. Publication does not
mean consumer execution completed. No dispatcher, claim/lease mechanism, retries,
consumer deduplication storage or queue recovery is supplied by this change.

## Retention and migration

Pending events are retained. Published events remain until an explicit retention
policy is chosen; no cleanup job is installed. Published rows are deletable for
future maintenance, but this implementation does not delete them. Migration
0010 creates the table, constraints, pending index and guard. Downgrade refuses
a populated outbox to avoid silently discarding delivery intent or history.

Real PostgreSQL tests cover business/event/audit atomicity, flushed rollback and
cancellation, failure in either side of the transaction, schema validation,
snapshot isolation, deterministic bounded reads, index availability, immutable
content, acknowledgment and migration round trips. Queue dispatch belongs to
issue #12; operation state, attempts and recovery remain separate work.
