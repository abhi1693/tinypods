# Chart verification

Repository sync records upstream metadata. Before consuming chart configuration, an
instance owner can call `POST /api/v1/chart-versions/{id}/verify` with the normal
session and CSRF header. The response contains the verified name, exact version,
SHA-256 digest, archive byte count, member count, Chart.yaml metadata, values.yaml
defaults and optional values.schema.json. These are configuration facts, not proof
that a chart is trustworthy, deployable or compatible with a cluster.

Helm sources select an exact version and digest in the current index, resolving
relative archive URLs at the final index location, including index redirects. OCI sources fetch the
stored chart-layer digest directly from the registered path; mutable tags are
never used to choose replacement content. Missing pins, changed bytes, disabled
repositories and source edits during verification fail explicitly. Authorization
is revalidated after external I/O. No database migration or persisted verification
status is introduced.

The shared async HTTPS transport checks public DNS, pins connections, verifies TLS,
limits redirects and strips authorization across hosts. Optional credentials are
host-scoped. The full operation uses the configured repository deadline. Downloads
are limited to 16 MB, actual gzip expansion to 64 MB, archive members to 4096 and
path/document depth to 32. Individual configuration documents are limited to 2 MB.
YAML aliases, duplicate keys, non-JSON values, links, devices and unsafe archive
paths are rejected. Archives are inspected in memory off the event loop, without
extracting files or executing templates, hooks or plugins. Packaged dependencies
remain opaque and count toward the outer archive limits; they are not recursively
expanded. Dependency verification and release validation are subsequent work.

Chart files remain externally hosted. This action changes no release defaults,
creates no deployment, and retains no archive. The API is owner-only and uses the
standard five-per-minute action budget. Defaults may contain upstream-provided
sensitive values; handle responses as configuration, and do not log their bodies.
