# Cluster capability discovery

Capability discovery reads selected facts using the cluster's active kubeconfig
and official native async Kubernetes SDK. Only instance owners can access the
results. No Kubernetes resources are created or changed.

## API

- GET `/api/v1/clusters/{id}/capabilities` reads a valid current JSON snapshot
  from Valkey, otherwise collects live.

Responses carry `source` (cache/live), `cached`, and a typed `snapshot` with
cluster ID, credential-version ID, start/collection timestamps and six sections:
Kubernetes version, API resource discovery, nodes, storage classes, ingress
classes and runtime classes. Each section reports available, unsupported,
forbidden, unavailable or not-collected, plus reason and completeness.
A successful empty list is available. Limits never imply completeness.

A cache miss, invalid schema, wrong cluster/credential identity, expired key or
cache outage triggers live discovery. A failed cache write still returns fresh
results with `cached=false`. Source failure is distinct from cache failure;
partial section successes remain usable. No old snapshot is relabeled fresh.

## Cache and concurrency

One namespaced schema-versioned JSON key is retained per cluster. The existing
metadata cache TTL applies (600 seconds by default, configurable 1–3600); the
existing cache byte limit applies (1 MB by default). Disabling cache keeps live
discovery available. This authenticated data does not use the public HTTP
response-cache route wrapper: authorization always runs before cache access.

Atomic timestamp comparison prevents an older refresh overwriting a newer one.
Database clock timestamps and fixed-width UTC serialization order independent
API processes. Current credential scope and revocation are rechecked before
return/publication. In-flight revocation cannot retroactively recall a response.

At most four live discoveries run concurrently in one API process, with a
two-second slot-acquisition deadline. Each discovery runs at most three sections
at once, with a 30-second section deadline and five-second request deadlines.
Each response is limited to 1 MB; lists request 20 objects per page and follow
at most five pages. API discovery expands at most 32 versions and retains at most
2000 resource descriptors. Cross-process duplicate discovery is allowed, so
Valkey locks are never required for live fallback.

The cache pool has bounded socket deadlines and a circuit breaker. Authentication
and rate limiting retain their existing independent failure rules. If the same
Valkey server is also used for mandatory rate limiting, its outage can reject
requests before discovery; use the dedicated cache URL when isolating cache
availability from security infrastructure.

## Meaning and permissions

Nodes report their Kubernetes name, labels, annotations, structured taints, architecture,
operating system/image, kernel version, capacity, allocatable resources, readiness and
administrative schedulability; these are not placement reservations or a capacity guarantee.
Storage classes report provisioner, default, expansion and binding mode. Ingress
classes report controller/default. Runtime classes report handler and scheduling
constraints. Advertised classes do not prove storage provisioning, routing, DNS
or runtime isolation work.

The credential needs GET discovery access (`/version/`, `/apis`, `/api/v1`
and advertised group/version discovery URLs), plus list access for nodes,
storageclasses.storage.k8s.io, ingressclasses.networking.k8s.io and
runtimeclasses.node.k8s.io. Forbidden sections are reported independently.
Raw responses, node addresses and credentials are excluded from snapshots. Requested
node labels and annotations are owner-visible; they are not logged. Runtime scheduling constraints are selected data,
not arbitrary object metadata.

No migration, durable history, scheduler, worker, queue, installation or active
provisioning probe is introduced.

Capacity and allocatable resources preserve Kubernetes quantity strings, including
CPU, memory, ephemeral storage, pod limits and extended resources. Allocatable is
not currently free capacity: workload requests and placement reservations must be
considered separately during provisioning. Final snapshots are bounded to 1 MB;
if needed, whole objects are removed in batches and that section is marked incomplete.
