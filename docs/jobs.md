# Durable jobs and RQ

TinyPods uses one PostgreSQL `jobs` table and RQ 2.6.0 on private Valkey.
The shared record has identity, a code-defined task, bounded JSON arguments,
optional related object/requester, status and timestamps. There is no separate
Operation, JobAttempt, lease, workflow engine or queue-intent outbox.

## Create, commit, submit

Trusted application code calls `tinypods_core.jobs.service.create_job` in the
same transaction as related business changes. It validates the registered task
schema, snapshots arguments, checks the optional target and leaves commit to the
caller. After a successful commit, await `jobs.queue.submit` with the stable UUID.
Never enqueue before commit, or roll back already committed business work because
queue delivery failed. `submit` borrows the process database but opens fresh
sessions; it never shares a request session with queue I/O.

RQ's synchronous client is isolated in a thread for async callers, with finite
socket/connect waits and no implicit Redis retries. Cancellation can leave that
bounded thread finishing a send: committed work remains recoverable. The worker's
blocking dequeue is owned by RQ itself. Network failure and full/noeviction queue
backpressure leave the Job pending and return false. A database failure after
accepted enqueue can still propagate; retrying a business request needs the
future domain idempotency contract rather than creating another Job blindly.

`GET /api/v1/jobs/{id}` returns safe status/timestamps under existing
platform-owner authorization. Requester identity and related object links do not
grant access. Argument JSON and raw errors are not exposed. There is no public
job-submission endpoint; concrete domains own their acceptance/authorization paths.

## Task and target contracts

`jobs.tasks.TASKS` is a code-owned mapping of stable task names to Pydantic input
schemas, native async handlers and RQ timeout/retry policy. Handlers receive the
validated input and detached Job metadata, including requester identity, and own
their domain-specific execution authorization. No production task is registered
yet. Tests register their own task in isolated processes, including real RQ and
Compose workers, without packaging that task in runtime images. Add concrete
domain tasks with explicit validation and behavioral regressions, not a client-supplied callable/import path. All RQ domain jobs invoke
`tinypods_core.jobs.execution.execute` with one canonical UUID string. The worker
loads and revalidates PostgreSQL arguments; handlers own current requester checks.

RQ uses JSON serialization, never pickle, on both producer and worker. Queue write
access remains trusted: RQ can resolve callable names, so JSON alone does not make
an untrusted Redis endpoint safe. Keep queue credentials private, isolate cache
traffic, and use a distinct `TINYPODS_JOB_QUEUE` per TinyPods installation sharing
Valkey. All producers and its worker must use the same name. No task stores secret
values; pass safe identifiers and resolve credentials inside an authorized handler.

`target_type`/`target_id` must both be set or both NULL. The former selects a
code-owned async resolver, never dynamic table SQL. The initial allowed target is
`auth.user`; new domains add a resolver when used, rather than pre-registering
speculative models. A Job has at most one target and a target can have many Jobs.
This is a logical reference, not a PostgreSQL FK. Validate domain access before
creation. Execution rechecks target existence and requester authorization. If a
target disappears, history remains and execution fails safely; no silent retargeting
or cascade deletion is invented. Stronger deletion locking belongs to concrete
side-effecting domains. Marketing email is an illustrative future task, not a
feature supplied by this change.

## Worker and recovery

`tinypods-worker` replaces the idle worker with a standard RQ worker and
`with_scheduler=True`. Start with one worker replica. `--burst` processes queued
work and exits; `--check` validates settings without contacting services.
Startup uses a disposable database lifetime to reconcile missed submissions and
ensures one RQ-scheduled recurrence exists. Every periodic run schedules its next
run through RQ even after an ordinary failure, following NetBox's small wrapper
pattern. This uses neither separate rq-scheduler/rq-cron services nor a custom
polling/retry timer. Schedule registration is designed for the initial single-worker
deployment, not an election or distributed singleton guarantee.

Configuration:

- `TINYPODS_JOB_QUEUE=tinypods`
- `TINYPODS_JOB_RECONCILE_MINUTES=30` (1 to 1,440)
- `TINYPODS_JOB_BATCH_SIZE=100` (1 to 1,000)

Recovery uses bounded UUID-ordered pages and RQ continuation jobs so active early
rows cannot starve later work. It checks RQ before resubmitting pending or queued
work missing from Valkey. RQ owns delayed delivery and retries according to each registered task policy. Running or
terminal deliveries do not re-execute application work. A conditional database
transition prevents two deliveries from both claiming pending/queued work; it is
not a renewable lease or a guarantee against conflicting external effects.

If execution raises an ordinary exception, PostgreSQL returns to pending while RQ
has retries left, or becomes failed when exhausted. Failures forwarded to RQ have
fixed diagnostic text, not raw task exceptions. Hard worker death, cancellation,
queue loss or uncertain completion can leave running state. Once RQ no longer
reports active work, reconciliation marks such work failed for operator review;
it does not automatically repeat uncertain external effects. Database failures
around completion can also require this review. Failed/cancelled jobs are not
silently resurrected. Generic foundation does not yet expose cancellation control.

PostgreSQL commit and Valkey enqueue are **not atomic**. Accepted queue work can be
lost with Valkey, or replayed after interrupted acknowledgment. Stable UUIDs and
claim checks help with duplicates but do not promise exactly-once effects. Future
tasks must make effects duplicate-safe and reconcile uncertainty before registering
side-effecting lifecycle handlers. RQ metadata alone is not durable business state. Queue reset also loses RQ retry
counters; no independent durable retry budget is claimed.

A missed prompt submission normally waits up to approximately the configured
interval, plus backlog/job duration/outage. RQ's scheduler polls **Valkey**, not
PostgreSQL, at its own cadence. No per-second idle PostgreSQL scan is introduced.
A hard kill can bypass the recurrence wrapper; worker restart repairs it. Valkey
reset can erase both jobs and schedules: restart the worker after restoring it to
reseed and reconcile. This is not independent recovery while the only queue is down.

## Migration and scope

This feature is undeployed. Revision `0010_jobs` replaces the superseded
`0010_transactional_outbox` migration and its unused helper/tests/docs; there is no
compatibility layer or legacy table retained. Fresh databases and upgrades from
`0009_application_installations` are supported. No operator database is rewritten
or reset. An experimental database already stamped with the removed revision must
not be upgraded blindly; inspect its contents and choose an explicit local reset
or reviewed data migration. Downgrade refuses populated Job history.

This is the minimal foundation for #11/#12, not completion of their advanced
requirements. Domain request idempotency, richer attempt ownership and scoped
history remain follow-up work where needed. #13 owns per-app coordination,
checkpoints, cancellation around side effects and uncertain Helm reconciliation.
#18 still gates Helm plans/approval before mutation. No cluster change is supplied.

NetBox v4.4.5 (commit 43cb47622392d753bcf3a1485672c235775950e1) informed
post-commit enqueue and worker-managed recurrence. The implementation is original
FastAPI/SQLAlchemy code, not copied Django wrappers. NetBox is Apache-2.0; any future
code copying must preserve its applicable license/attribution/modification notices.

## Resource qualification

Compose retains the existing default 0.5 CPU / 256 MiB worker limit. RQ has one
worker parent, its built-in scheduler child and a transient job child; no extra
broker/listener/cron process is added. The process tree, not just the parent, must
fit the allocation. Test 24-hour idle, bursts, queue outage/full state, DB restart,
worker kill/restart and queue reset on the intended Raspberry Pi (at most three
CPUs and a few GiB RAM). Record RSS/CPU and Valkey command rate and verify no busy
PostgreSQL polling or sustained memory growth. Development-host tests are not a
claim that these hardware acceptance checks passed.
