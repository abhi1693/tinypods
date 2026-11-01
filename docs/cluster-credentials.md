# Cluster credentials and connection testing

This phase implements cluster scope from the secret-storage plan. Application
scope waits for installed-application records and real namespace bindings.

```mermaid
classDiagram
    Cluster "1" -- "0..1" ClusterSecret
    ClusterSecret "1" -- "1" Secret
    Secret "1" -- "1..*" SecretVersion
    ClusterSecret --> SecretVersion : active version in the same secret
    class SecretVersion {
        UUID id
        UUID secret_id
        int version
        bytes encrypted_payload
        bytes nonce
        string encryption_key_id
        datetime revoked_at
    }
```

The cluster association is required at commit time. Its active-version composite
foreign key prevents selecting another secret's version. Cluster deletion is
protected while credentials exist. Credential replacement creates a new logical
version; activation can select an existing non-revoked version. Revocation blocks
new resolution and does not revoke credentials already accepted by Kubernetes.

The existing cryptography library provides AES-256-GCM with fresh 12-byte nonces.
Authenticated context binds secret identity, version identity/number and cluster
identity. Original bytes are encrypted directly. Keys live in an operator keyring
outside PostgreSQL. Rotation changes encryption keys without changing logical
version IDs and runs in bounded, resumable CLI batches. Missing keys and failed
authentication tags fail closed. Keep backups of required old keys until rotation
is complete and backup recovery has been verified.

Owner-only versioned APIs return nested public metadata through the shared
presenter. Kubeconfig is write-only; neither metadata, logs, audit snapshots nor
errors include the confidential document. There is no plaintext read endpoint.
Future application payloads will use the same byte-encryption boundary with their
own actual scope associations.

The manual connection test uses native async HTTP and verified TLS. Embedded
certificate authority/client certificate/key data and bearer tokens are supported.
External files, executable authentication plugins, proxies, redirects and insecure
TLS are rejected. The current context chooses the endpoint; private LAN endpoints
are intentional for self-hosted clusters. Only instance owners may set or test it.
An authenticated SelfSubjectReview precedes version discovery, so a publicly
readable version endpoint cannot create a false authentication success.

Tests cover ciphertext and exact byte round trips, scope/tamper/revocation denial,
concurrent version creation, active-version isolation, key rotation, authorization,
public schemas, audit/log leakage, real TLS token and client-certificate exchanges,
timeouts, cancellation and metadata-only errors. No queue or background worker
infrastructure is introduced. One migration owns this phase's schema changes.

Protocol references: [authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/),
[kubeconfig](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/), and
[authenticated identity review](https://kubernetes.io/docs/reference/access-authn-authz/authentication/#api-access-to-authentication-information-for-a-client).

## Operator configuration

`TINYPODS_SECRET_KEYS` is a JSON mapping of stable key identifiers to Base64-encoded
32-byte keys. `TINYPODS_SECRET_ACTIVE_KEY_ID` chooses the key for new writes. Both
are optional together; without them credential writes/resolution fail closed while
metadata and other application features remain available. Incomplete/invalid
keyrings fail settings validation at process startup. A missing historical key
fails that version's resolution or rotation; metadata remains accessible for recovery.
Compose forwards both settings. Keep real values in ignored operator configuration.

Configure the new active key alongside all old read keys, then run
`tinypods secrets rotate-key --batch-size 100` repeatedly. Each invocation commits
one batch and reports `updated` and `remaining`. A failed batch rolls back. Zero
remaining covers the current database, not old backups or external credential
revocation. Keep old keys until the required recovery window is covered.

## API sequence

All routes are under `/api/v1` and require a live instance-owner session. Unsafe
calls also require the existing CSRF header. Read DTOs use nested public metadata.

1. Create `/cluster-secrets` with `cluster_id`, `name`, optional `description`, and
   write-only `kubeconfig`. The first encrypted version becomes active.
2. Read `/cluster-secrets?cluster_id=...` and `/secret-versions?secret_id=...` for
   association and version metadata. Confidential columns stay deferred.
3. POST `/secret-versions` with `secret_id`, write-only `kubeconfig` and optional
   `activate` (defaults true). Set false to stage a version.
4. POST `/cluster-secrets/{id}/activate` with `version_id` to switch to an existing,
   non-revoked version of that exact secret.
5. POST `/clusters/{id}/testConnection` without a body for fixed connection diagnostics.
6. POST `/secret-versions/{id}/revoke` without a body to permanently block future
   resolution. Revoking the active version does not silently select an older one.

Before this phase cluster responses contained registry metadata only. That response
stays public metadata; credential administration uses its own owner-only resource:

```json
{
  "id": "association-uuid",
  "secret": {"id": "secret-uuid", "name": "Pi credentials", "description": "", "kind": "cluster"},
  "cluster": {"id": "cluster-uuid", "name": "Cluster 1", "slug": "cluster-1", "region": {"id": "region-uuid", "name": "Gurugram", "slug": "gurugram", "flag": null}},
  "active_version": {"id": "version-uuid", "version": 1, "revoked_at": null}
}
```

This abbreviated response omits unchanged audit timestamps and uses UUID
placeholders. There is no `kubeconfig`, token, private key, nonce or ciphertext in
this response. Connection success returns only `connected`, a fixed `reason`, and
`kubernetes_version`. Transport failures return fixed diagnostic categories.

Python's TLS client-certificate loader requires file paths. It loads embedded
material through exclusive 0600 files in a private temporary directory and removes
them before network I/O. The standard Compose runtime mounts `/tmp` as tmpfs.
Outside that runtime, use an appropriately protected memory-backed temporary
filesystem if avoiding all disk copies is required. Process memory and TLS state
remain sensitive; Python does not provide guaranteed memory erasure.

Switch every credential-writing process to the new active key before relying on a
final zero-remaining count. The rotation command does not update other processes'
configuration or fence a stale writer. Keep old keys available during the rollout,
repeat verification after writers have switched, and cover retained backups before
retirement. An active credential can remain in flight while it is revoked; future
resolution is blocked, while external credential revocation is a separate operation.

The test requires permission to create an `authentication.k8s.io/v1`
`SelfSubjectReview`. It confirms verified TLS, authenticated identity and version
discovery. Deployment permissions and cluster readiness require their own checks.
Use an endpoint reachable from the backend; a kubeconfig loopback address refers
to the backend's host/container. Server names must match the TLS certificate.

Generate an independent operator key with the installed encryption library, then
store its output in protected, ignored operator configuration:

```sh
uv run --no-sync python -c "import base64; from cryptography.hazmat.primitives.ciphers.aead import AESGCM; print(base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode())"
```

For example, use `TINYPODS_SECRET_KEYS={"primary":"<generated-key>"}` and
`TINYPODS_SECRET_ACTIVE_KEY_ID=primary`. The placeholder is not a usable key.
Base64 is used for key transport; confidential payload bytes are encrypted directly.
