# Nested API representations

Read responses include related public objects under their relationship names.
Writes and query filters continue to accept explicit UUID fields such as
`repository_id`, `chart_version_id`, `parent_id`, and `region_id`.

For example, `GET /api/v1/charts/{id}` previously returned:

```json
{"id": "chart-uuid", "repository_id": "repository-uuid", "name": "redis", "available": true}
```

Its relationship now appears as:

```json
{"id": "chart-uuid", "repository": {"id": "repository-uuid", "name": "Bitnami", "source_type": "helm_repository"}, "name": "redis", "available": true}
```

These abbreviated examples omit unchanged audit timestamps. UUID placeholders are
illustrative. POST and PATCH inputs do not accept nested relationship objects.

## Declaring representations

Each public resource model declares `brief_fields`, a tuple of public response
field names. `Related[ResourceRead]` uses that selection to build a reusable typed
nested schema. A missing or empty tuple uses the resource's full **public DTO**;
it never serializes arbitrary persistence fields. Private paths, password hashes,
sessions, credentials and provider internals remain excluded.

Full read DTOs register their persistence model; write DTOs remain separate.
Declarations are checked against public fields. Recursive full fallback graphs
are rejected instead of creating unbounded responses. Keep brief definitions
small and finite. The same generated schema supplies OpenAPI field definitions.

## Loading relationships

The shared presenter compiles the selected schema into SQLAlchemy loading options:
single relationships use joins, collections use select-in batches. Selected
scalar columns and foreign keys are loaded together. Undeclared scalar and
relationship access raises before a lazy query can occur. Projection performs no
SQL. Collection queries depend on the graph and bounded batch sizes rather than
one query per returned object.

CRUD services retain authorization, validation, audit and transaction behavior.
A shared API adapter loads authorized page identities and projects read DTOs for
list, detail and write responses. Action results use the same presenter.
Computed values, including inherited region flags, resolve in batches across the
whole loaded graph. Region responses expose only `flag`; clearing a write override
still restores inheritance.

Authentication responses place the public identity under `user`, containing
`id`, `username` and `is_owner`. Session expiry fields stay at the response root.
No authentication permissions or cookie behavior change.

## Verification

Presentation tests compare one-record and 100-record pages, verify identical SQL
counts, and assert that pure serialization performs no queries. Existing CRUD,
permissions, source synchronization and hierarchy tests cover the revised response
contracts. The region benchmark also uses the shared nested projection path.
Demo records and one-off population scripts are excluded from version control.

A disposable PostgreSQL measurement of the presentation stage (excluding HTTP,
domain list selection and fixture setup) produced:

| Graph | Rows | SQL statements | JSON bytes | Elapsed ms |
| --- | ---: | ---: | ---: | ---: |
| Release, application, chart version and chart | 1 | 1 | 557 | 21.32 |
| Release, application, chart version and chart | 100 | 1 | 55,790 | 17.12 |
| Application/category membership | 1 | 1 | 265 | 7.84 |
| Application/category membership | 100 | 1 | 26,680 | 13.42 |

These are individual local observations, not throughput guarantees; cold query
compilation can make the single-record sample slower. Automated assertions check
query counts and correctness rather than timing thresholds. Collection fixture
graphs use two statements at both page sizes. Cluster pages additionally batch
inherited flags. JSON grows with returned records while SQL counts remain bounded.
