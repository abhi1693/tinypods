# Audit performance evidence

Measured on the build machine on 2026-11-01 using Python 3.13.18, Linux x86_64
and the disposable pinned PostgreSQL fixture. Reproduce with:

```sh
uv run --no-sync pytest tests/test_audit_performance.py -s --no-cov
```

The test writes `reports/audit-performance.json` with timings, traced Python peak
allocation and categorized actual cursor calls. Before-optimization measurements
are retained in `reports/audit-performance-before.json` for this work session.
No operator database is used. There are no timing pass/fail thresholds.

## Full transaction measurements

These sample runs include constructing/loading 1,200 ORM users, safe snapshot
reads, temporary staging checks/DDL, batches and commit. Both use allocation
tracing, which adds overhead; single-run times are diagnostic, not throughput or
latency guarantees. Cursor-call reduction is the more reproducible result.

| Case                          | Before cursor calls | Batched cursor calls | Before seconds | Batched seconds |
| ----------------------------- | ------------------: | -------------------: | -------------: | --------------: |
| Audited insert, 1,200 objects |               1,241 |                   41 |           4.32 |            3.12 |
| Audited update, 1,200 objects |               2,442 |                   54 |           4.48 |            2.26 |

Unaudited comparison transactions took 0.79 seconds for insertion and 0.45 seconds
for update in the batched run. Thus audit capture still has measurable overhead;
serializer-only results do not establish total capture scalability.

Original per-object reads accounted for 1,200 SELECTs on insertion and 2,400 on
update. Safe reads now group by model in batches of 100: 12 SELECTs on insertion,
24 on update, plus the initial user-load query. Staging catalog checks dropped
from 14 to 2 per transaction. Each case still creates one temporary table, writes
12 staging upsert batches and 12 final-snapshot update batches, and performs one
atomic final INSERT/SELECT. Stable UUID type identities are cached per immutable
registration. These are implementation optimizations within the approved design.

Traced Python peaks in the batched run were approximately 4.31 MiB for audited
insertion versus 3.73 MiB for the unaudited case, and 4.15 MiB versus 3.60 MiB for
updates. This does not include PostgreSQL/server memory or all native allocations.
The ORM retains normal unit-of-work references; database staging grows with the
transaction, while Python snapshot dictionaries are processed in batches. There
is no object-count cap or history truncation.

## Initialization and serialization

10,000 `ModelRegistration` constructor calls took about 7.4 ms with zero database
calls. This measures the generated dataclass constructor, not a cold interpreter,
module import, database startup or complete mapper configuration. A separate hot
path check measures repeated cached type-ID access.

Ten serializations of a loaded 10,000-member relationship took about 0.61 seconds
total under tracing, with a 1.70 MiB traced peak and zero database calls. Declared
relationships must already be loaded; uninitialized ones are rejected rather than
silently causing N+1 loading. Sorting is O(m log m) and copied identities/snapshots
consume O(m) memory. Large relationship collections still have real CPU/memory
cost even though they perform no I/O.

The full tests assert bounded SQL query counts, complete snapshot membership,
repeated-flush coalescing, rollback and transactions beyond 1,000 objects. They do
not claim multi-process throughput, p99 latency or an unlimited memory footprint.
Any future move away from same-transaction capture or PostgreSQL staging would be
an architectural decision to discuss separately; none was made here.
