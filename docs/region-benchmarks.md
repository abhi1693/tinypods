# Region inheritance benchmark

Comparison of baseline recursive reads, indexed recursion, and the stored-ancestry implementation. These are reproducible local measurements, not production latency guarantees.

## Environment and workload

- PostgreSQL 17.11, Python 3.13.18, x86_64, host reporting 16 logical CPUs.
- Disposable databases and synthetic data only. Existing application services and operator data were not modified.
- Five shapes: 10,003-node wide/balanced trees; a depth-1,000 chain; one deep detail; and a deep chain with overrides every 100 levels. Pages contain 100 regions except the single detail.
- Three query strategies, each with 20 timed requests at one reader, 64 at eight readers, and 128 at 32 readers, plus warmup requests.
- Additional ORM/JSON projection, one/four concurrent subtree writers, and held uncommitted flag/move transactions. Across both modes, over 12,000 read requests checked returned identities and flags.
- Preparation-disabled measurements model the driver policy used with transaction pooling, but run directly against PostgreSQL to isolate query behavior. Actual PgBouncer correctness is tested separately; these are not pool-capacity benchmarks.
- Query latency includes result consumption. ORM/JSON measurements additionally load public metadata and serialize DTOs; they exclude HTTP and authentication. Private ancestry stays deferred.
- Samples come from one host/run per mode; p95 estimates are sensitive to sample size and scheduling. Complete EXPLAIN ANALYZE/BUFFERS plans and distributions are saved by the script under reports/benchmarks/.

## Automatic preparation

Flag lookup p95 in milliseconds. Each cell shows **1 reader / 32 readers**.

| Shape                | Baseline recursion | Indexed recursion | Stored ancestry |
| -------------------- | -----------------: | ----------------: | --------------: |
| wide-10000           |       5.18 / 47.20 |      3.70 / 52.11 |    2.95 / 33.15 |
| balanced-10000       |      7.55 / 134.39 |      4.36 / 56.50 |    2.77 / 37.77 |
| depth-1000           |    145.36 / 150.41 |      6.35 / 57.77 |   20.32 / 63.76 |
| detail-depth-1000    |     89.19 / 302.81 |      5.41 / 19.79 |    1.89 / 11.59 |
| overrides-depth-1000 |    152.44 / 156.84 |      4.08 / 60.06 |   22.70 / 73.10 |

Additional stored-ancestry controls, p95 milliseconds. ORM/JSON cells show **1 / 8 / 32 readers**. Mixed reads use 16 readers while four writers move the shared subtree; writer latency includes the whole transaction and its path cascade.

| Shape                |              ORM + JSON | Reads with four writers | Worst writer p95 | Reads during held move |
| -------------------- | ----------------------: | ----------------------: | ---------------: | ---------------------: |
| wide-10000           | 12.84 / 145.62 / 193.59 |                   17.30 |           371.67 |                   9.16 |
| balanced-10000       |  10.10 / 54.11 / 200.32 |                   17.23 |           296.59 |                  10.10 |
| depth-1000           |  25.83 / 51.86 / 307.17 |                   33.93 |           173.04 |                  26.35 |
| detail-depth-1000    |    6.11 / 23.21 / 83.82 |                   10.01 |           179.46 |                   4.69 |
| overrides-depth-1000 | 29.81 / 193.96 / 190.77 |                   43.19 |           211.77 |                  31.67 |

## Preparation disabled

Flag lookup p95 in milliseconds. Each cell shows **1 reader / 32 readers**.

| Shape                | Baseline recursion | Indexed recursion | Stored ancestry |
| -------------------- | -----------------: | ----------------: | --------------: |
| wide-10000           |       6.35 / 51.40 |      5.16 / 53.56 |    2.80 / 32.64 |
| balanced-10000       |      5.33 / 142.92 |      3.58 / 57.10 |    3.10 / 37.52 |
| depth-1000           |     61.18 / 171.16 |      7.57 / 58.85 |   19.16 / 61.84 |
| detail-depth-1000    |     90.23 / 327.32 |      4.70 / 18.44 |    1.78 / 13.31 |
| overrides-depth-1000 |     60.70 / 160.96 |      3.83 / 55.54 |   24.11 / 66.26 |

Additional stored-ancestry controls, p95 milliseconds. ORM/JSON cells show **1 / 8 / 32 readers**. Mixed reads use 16 readers while four writers move the shared subtree; writer latency includes the whole transaction and its path cascade.

| Shape                |              ORM + JSON | Reads with four writers | Worst writer p95 | Reads during held move |
| -------------------- | ----------------------: | ----------------------: | ---------------: | ---------------------: |
| wide-10000           | 13.85 / 155.55 / 199.64 |                   18.25 |           368.77 |                  11.54 |
| balanced-10000       |   9.79 / 52.79 / 200.69 |                   24.95 |           253.20 |                   9.37 |
| depth-1000           |  27.73 / 54.85 / 302.04 |                   34.87 |           178.17 |                  30.36 |
| detail-depth-1000    |    4.79 / 21.54 / 82.50 |                   10.61 |           180.23 |                   4.12 |
| overrides-depth-1000 | 29.75 / 190.16 / 195.89 |                   50.90 |           203.26 |                  39.21 |

## Interpretation

- Stored ancestry improves the shallow-tree and single-deep-region workloads. Indexed recursion remains faster for 100-region pages at extreme depth; the measurements do not establish a universally fastest strategy.
- The stored path removes recursive traversal from normal reads and supports predictable inheritance after moves. Parent traversal occurs only during structural writes to update descendants.
- Structural writes are serialized before row/FK locks. Large subtree moves rewrite descendants and can take hundreds of milliseconds or delay other structural writers; they are manual administrative changes. Reads and visual metadata edits do not acquire the structural lock.
- Every held-write read completed without waiting for transaction release and returned committed flags. Separate direct/PgBouncer tests cover crossing moves, insert-versus-move races, and the actual API service lock order.
- An inverted index over every ancestry element was rejected during exploration because repeated deep subtree rewrites caused excessive index maintenance. The final design uses an ordinary parent index and a partial ID index for flag-bearing ancestors.
- Trigger restoration under an empty search path and rejection of direct path/identity edits have regression tests. The benchmark does not prove failure-free operation under every workload or replace deployment sizing.

## Reproduce

```sh
uv run --no-sync python scripts/benchmark_region_flags.py
uv run --no-sync python scripts/benchmark_region_flags.py --unprepared
```
