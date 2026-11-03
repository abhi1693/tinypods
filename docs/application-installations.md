# Application installations and configuration history

An Application is a catalog entry. A Release is a TinyPods definition pinning a
ChartVersion (the upstream Helm package version and digest). An
ApplicationInstallation reserves an intended target; an InstallationRevision
saves validated configuration. These APIs do not deploy or mutate Kubernetes.

An installation has a fixed owner, cluster and namespace. Its descriptive name
can change. The reserved Helm name is derived as `tp-` plus the installation
UUID without hyphens, giving independent names within a cluster/namespace.
The single nullable `revision_id` database field identifies currently deployed
configuration. Creating an installation leaves it null, and saving revisions
does not change it. Future successful execution may advance it; clients cannot.
The public representation exposes this pointer as the nested `revision`.

Cluster registration currently has no per-user grants: selecting a cluster to
create an installation requires platform-owner privileges, matching cluster
administration. Installation reads, history and application secrets use the
stored owner; another user cannot access them, even with platform-owner status.

## API walkthrough

Use an authenticated session and its bound CSRF header for writes. Foreign keys
are UUID inputs; responses use the same nested related-resource convention as
the catalog APIs. For example, after choosing an available published release
and cluster, send:

```http
POST /api/v1/installations
Content-Type: application/json

{"name":"Monitoring","cluster_id":"00000000-0000-0000-0000-000000000001","namespace":"monitoring"}
```

Use the returned installation ID in subsequent paths. The following examples
assume the release declares a user-controlled numeric `replicas` field:

```http
POST /api/v1/installations/{installation_id}/validation

{"release_id":"00000000-0000-0000-0000-000000000002","overrides":{"replicas":2}}
```

Validation returns bounded codes and paths without rejected values and does not
save anything. To append the first revision:

```http
POST /api/v1/installations/{installation_id}/revisions

{"release_id":"00000000-0000-0000-0000-000000000002","overrides":{"replicas":2}}
```

Each valid save appends independent immutable history. The service uses a short
automatic database lock to keep the installation's application and secret scope
consistent. Saving configuration leaves the deployed revision pointer unchanged.
History is ordered newest first.

Read history with `GET .../revisions?limit=25&offset=0` and a specific revision
with `GET .../revisions/{revision_id}`. There are no revision update,
delete or deployment endpoints. `PATCH /installations/{id}` accepts only `name`.

The selected release is immutable and must be published and non-revoked for new
validation. Ordinary access also requires its catalog application to be
published. Revisions retain overrides (explicit user choices) and resolved values
independently of future catalog edits/defaults. Related catalog metadata remains
a live representation, not a publication snapshot. Historical reads need no
chart download, upstream availability or current release eligibility.

## Application secrets

Create the installation before creating its secrets. `POST .../secrets` accepts
`{"name":"Database password","payload":"<write-only UTF-8 payload>"}`; it returns
safe association metadata, never the payload. The `secret.id` in this response
selects the secret. List initial/history metadata with
`GET .../secrets/{secret_id}/versions`; append with
`POST .../secrets/{secret_id}/versions` and `{"payload":"<new payload>"}`.
The decoded byte limit is 262144. Use
`POST .../versions/{version_id}/revocation` for permanent revocation.

Reference an exact logical version in a validation/revision request:

```json
{
  "release_id": "00000000-0000-0000-0000-000000000002",
  "overrides": { "replicas": 2 },
  "secrets": [
    {
      "version_id": "00000000-0000-0000-0000-000000000003",
      "target": "database.password",
      "use": "value"
    }
  ]
}
```

The target must be a declared user-controlled sensitive/password field.
Confidential literals are rejected from configuration inputs and database
snapshots. The existing encrypted store decrypts transiently for bounded schema
validation; confidential targets are removed before values are persisted.
Helm globals/imports that propagate payloads into another saved value are
rejected. No suffix-based secret detection or separate encryption system is used.
Opaque user text in fields not declared confidential is ordinary configuration;
catalog authors must correctly classify confidential controls.

Application ciphertext authenticates the actual installation, cluster and
namespace with a distinct context. Existing cluster ciphertext/AAD is unchanged.
The existing operator key-rotation command rotates both kinds without changing
logical version IDs. Revoked versions cannot be selected for new revisions;
existing history retains its exact association. Secret-version deletion is
restricted when referenced. Payloads are excluded from read DTOs, audit snapshots
and log calls.

Kubernetes Secret references are separate:

```json
{
  "namespace": "monitoring",
  "name": "database-credentials",
  "key": "password",
  "target": "database.existingSecret",
  "use": "existingSecret"
}
```

Supply these in `kubernetes_secrets`. The target must be a user-controlled
non-confidential configuration field selecting an existing Secret name. Namespace
scope and syntax are validated; name, key, target and use are saved as a separate
reference. This scope does not deliver encrypted values to Kubernetes.
Validation checks reference syntax and installation namespace scope.
It does not query live objects or verify their required keys.

## Guarantees and limits

Revisions and their reference associations are immutable at the database
boundary. Composite constraints reject a deployed pointer belonging to another
installation. Serialized revision creation fixes application lineage on the first
saved revision, and historical installations/releases/versions have restrictive
foreign keys.

Chart retrieval, secret decryption/key-file reads and bounded CPU/schema work happen
outside database sessions. Secret-version scope checks use one ordered batch query.
Built-in credential revalidation borrows the operation's connection, including
with a one-connection pool; it does not require a second checkout.
The final short transaction locks the installation and rechecks authorization,
release/source eligibility and exact secret-version scope/revocation, then saves the revision, bindings and audit atomically.
Failure/cancellation rolls back; new validation can fail when upstream bytes
cannot be retrieved. Digest pins detect changed bytes but do not guarantee
upstream retention.

Configuration validation does not establish cluster capacity, deployment
permissions or runtime readiness. Rendering/apply, approvals, operation queues,
upgrades, backup, frontend screens and complete secret delivery/lifecycle work
remain separate. Issue #9 remains open.
