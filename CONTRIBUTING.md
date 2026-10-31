# Contributing

These notes are engineering guidance for a consistent, maintainable codebase. Use judgment: choose the simplest design that preserves the relevant contracts, and explain material tradeoffs in the PR. The aim is readable code and dependable behavior, not uniformity for its own sake.

## Shape the change

Keep entry points thin and put reusable application behavior in the core package. Prefer cohesive modules and clear ownership over scattered helpers, duplicate logic or abstractions without a working use case. Classes are useful when they own state or a lifecycle; ordinary functions are useful for transformations.

A focused issue and PR should explain the problem, changed behavior and validation. Natural-language commits with the issue number are preferred. Avoid placeholder packages, unrelated cleanup and changes to operator data.

## Async I/O and resources

Use native async clients for network/database operations reached from async request or worker paths. Await connection, session and pool cleanup. Bound external waits and consider cancellation, retries and partial failure explicitly. Keep sessions local to one request/job; a shared session is not a concurrency mechanism.

Synchronous CLI, migration and build operations can stay synchronous. Small JSON transformations and hashing are CPU work, not network I/O. If a library is inherently blocking on an async path, isolate the boundary and explain its ownership and limits rather than hiding it inside an async function.

Prefer context managers and explicit resource owners. Cleanup should still run when another resource fails. Cancellation should propagate after necessary bounded cleanup.

## Types and public contracts

Annotate parameters and return values precisely, including test helpers. Use domain types instead of broad object/Any where the actual shape is known; dynamic library boundaries sometimes justify those types. Keep public request/response DTOs separate from persistence models, settings and credentials.

Runtime constants and type expressions have different roles. Use literal values, literal type aliases or enum members inside Literal[...] rather than substituting arbitrary Final variables. The [typing specification](https://typing.python.org/en/latest/spec/literal.html#legal-and-illegal-parameterizations) defines the accepted forms; runtime validation alone does not prove an annotation is valid.

Give routes meaningful descriptions and stable operation IDs. Declare response/status semantics and constrain inputs where useful. The [API guide](apps/api/README.md) covers route-specific conventions. Scalar path/query values do not need artificial wrapper models, and read-only routes do not need invented request bodies.

## Documentation and comments

Use Google-style docstrings for classes, functions and methods. Explain responsibility and observable behavior, with Args, Returns or Yields, and Raises sections where relevant. Describe what each input and output means; avoid simply repeating its type. Document ownership, side effects, mutation, concurrency and cleanup when callers need to preserve an invariant.

Straightforward helpers need proportionate explanations; complex operations need enough context for a reader unfamiliar with their implementation. Comments are most useful for why an ordering, bound, atomic operation or cancellation shield matters. Avoid narrating each line or recording conversational/design history in code.

## Error boundaries and security

Prefer validation and clear control flow to exceptions used as ordinary branching. Catch errors narrowly at boundaries that can recover or translate them. Process-level error handling, optional cache fallback and cancellation-safe cleanup are useful boundaries; they should not swallow unrelated source errors or repeat side effects.

Choose log fields deliberately. Keep credentials, tokens, raw configuration and sensitive request/exception data out of log calls at the source. Formatters do not scan or redact arbitrary keys or values. Preserve safe configuration errors, deliberate public DTOs and SecretStr/SecretBytes safeguards. Repository scanners do not sanitize runtime logs. Keep .env and other operator credentials uncommitted.

Logging preserves complete supported values without size-based truncation. Cycles, unsupported objects and non-JSON numeric/binary values have explicit representation markers. Normalization snapshots mutable values on the producer; rendering and output run on the API logging thread. Full-content snapshots consume proportional CPU/memory, and the bounded queue still drops whole records when full. Output failures and bounded shutdown can also lose records; do not describe this as durable or lossless delivery.

Keep reused protocol values in the owning domain module, such as logging/constants.py, http/constants.py or cache/constants.py. Prefer typed enums, tuples/frozensets and frozen records where they capture stable identifiers or owned snapshots. JSON payloads, library configuration mappings and TypedDict serialization boundaries should remain dictionaries; avoid a global constants dumping ground or classes for one-off values.

Preserve private infrastructure, least privilege and fail-closed authorization/isolation behavior. Shared request caching is explicitly opt-in for public data, not an authorization mechanism. Document actual safety limits rather than claiming a blanket guarantee.

## Versions and generated files

VERSION is the maintained project version. Synchronize generated package metadata with scripts/sync_version.py, refresh the lockfile when needed, and keep installed/runtime versions aligned.

Regenerate openapi.json when public routes, DTOs, constraints or descriptions change. The always-run CI freshness check compares a new application schema with the committed artifact, including nested routers. Internal changes with an identical contract need no artificial JSON diff.

Generated migrations remain outside strict typing. Review schema operations and add a migration when the schema changes; avoid mechanically rewriting historical generated files. Generated API clients and schema snapshots should be regenerated through their tools.

## Validation

Use regressions that demonstrate the changed behavior or failure mode. Real-service tests are useful for transaction boundaries, pooling, cache ownership/expiry and network failures. Keep fixtures isolated and disposable, and avoid brittle timing assumptions or tests that merely restate the implementation.

Run the [repository checks](README.md#checks), including lint/format, strict backend typing, version/OpenAPI checks and the relevant tests. Finish the full integration suite before handoff when the change crosses shared runtime boundaries. Report the latest CI result, coverage and any untested limitation accurately.

## Formatting and linting

Ruff owns Python formatting and linting. The existing PR workflow runs both
`ruff check .` and `ruff format --check .` using the locked development dependencies.
The pre-commit hooks use the same Ruff version and project rules, with automatic
fixes enabled locally. Generated migrations and build/vendor output are excluded
from hooks; historical generated migrations remain outside the typing audit.

Install hooks with `uvx --from pre-commit==4.3.0 pre-commit install`.
Run the complete hook check with
`uvx --from pre-commit==4.3.0 pre-commit run --all-files` before committing.
If the locked Ruff version changes, update the hook revision alongside it.

## Coverage reports

Pytest writes statement and branch coverage to `reports/coverage.xml`,
`reports/coverage.json` and `reports/html/`, alongside JUnit results. The HTML
report shows executed and missing source lines and partial branches. Reports are
ignored generated artifacts. Node.js is needed for the workflow publisher tests,
which execute the actual inline reporting script with mocked GitHub APIs; it is
not a backend runtime dependency.

CI uploads these reports as `backend-tests-<attempt>`. For same-repository PRs,
a separate data-only job updates one coverage comment with the pass/fail gate,
minimum, test/source counts and workspace percentages linked to the tested commit.
It never checks out or executes PR code with comment-write permission. Fork and
Dependabot PRs retain downloadable reports without attempting a privileged comment.

The percentages measure the complete backend exercised by the suite, including
real-service tests. They are not changed-line coverage. Download the HTML/JSON reports for complete missing-line and branch detail. Missing or
inconsistent artifacts are reported as unavailable, never as passing coverage.
A report-only rerun may reuse a successful backend job artifact from an earlier
attempt; failed backend attempts must provide their own matching report.

The backend coverage gate requires 100% with branch measurement enabled. After the
full suite, run `uv run --no-sync python scripts/check_coverage.py`. This checks
exact statement/branch counts and missing-code lists rather than a percentage
rounded for display. Keep the existing API/core/CLI/worker scope intact; add
behavioral regressions or controlled fault injection rather than hiding reachable
code through exclusions.
