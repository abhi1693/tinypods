# External chart repositories

TinyPods stores external source configuration and synchronized chart metadata in
PostgreSQL. It does not host, upload, mirror or retain chart archives. Sync is an
explicit operator action, available through both the CLI and admin API.

## Records and release references

- ChartRepository: name, typed source_type, base URL, enabled flag and last successful sync.
  Source type defaults to `helm_repository`; the other value is `oci_registry`.
- Chart: repository foreign key, name/path and last-observed availability.
- ChartVersion: chart foreign key, version, package digest and availability.
- Release: application foreign key, chart-version foreign key and embedded configuration fields.

The migration backfills the old release strings into these records before
removing them. Existing releases keep their exact observed versions and digests.
Version identities are immutable. An upstream version whose digest changes
creates another observation; sync never repoints existing releases. Entries
missing from a successful snapshot become unavailable and remain in history.
A failed or stale concurrent sync does not replace the previous snapshot.

Deleting an unused repository removes its charts and versions in one transaction.
Any application release referencing a version blocks the entire deletion with
409. The service deletes children through the ORM for transactional audit capture;
database cascades and restrictive release references also enforce integrity for
direct SQL. Disabling a repository retains all metadata and prevents sync.

## API and CLI

Register a source through authenticated, CSRF-protected administration:

```text
POST /api/v1/chart-repositories
{"name":"Example","url":"https://charts.example.com"}
```

Provide the repository base URL, not its index.yaml file. For OCI, use
`source_type=oci_registry` and a base such as `oci://registry.example.com/team`.

```sh
tinypods repositories sync REPOSITORY_ID
tinypods repositories sync
```

The first form syncs one repository; omission syncs all enabled sources. Output
reports per-source results as JSON. Each source commits atomically. In an all-source
run, a later failure does not undo earlier successful sources; failures are
reported and produce a nonzero exit status. The local command is an operator
boundary using backend database configuration and an explicit system audit actor.
It does not impersonate a user or bypass HTTP authentication through an API route.

The admin equivalent is `POST /api/v1/chart-repositories/{id}/sync`, using the
shared detail-action authentication, CSRF and rate policy. Repository/chart CRUD
and read-only chart-version browsing use the standard resource APIs. Reads
require current credentials; writes/sync require installation-owner privileges.
Filter charts by repository_id and versions by chart_id; availability filters,
search and bounded pagination are supported. Link a selected version through
`chart_version_id` when creating/updating an application release. Legacy source
strings are no longer accepted in release requests.

Helm sync reads the repository index. OCI uses catalog discovery where exposed,
then tag lists and Helm manifest/config metadata. Some registries hide their
catalog: register known chart names through `POST /api/v1/charts` and sync their
versions. A registry without discovery or registered paths reports an explicit
discovery-unavailable result, not an empty successful catalogue.

## Network and credentials

Only HTTPS metadata requests to public addresses are allowed. Every DNS answer
is checked; connections pin an approved IP while preserving TLS SNI and Host
verification. Redirects are rechecked and cannot inherit cross-origin credentials.
Environment proxies are disabled. Metadata bytes, YAML depth/aliases, item/page
counts and operation time are bounded. YAML construction runs off the event loop.
No chart code is rendered or executed during synchronization.

Operator settings: `TINYPODS_REPOSITORY_ALLOWED_HOSTS` optionally restricts exact
hostnames; an empty list allows public HTTPS hosts. `TINYPODS_REPOSITORY_MAX_BYTES`,
`TINYPODS_REPOSITORY_MAX_ITEMS` and `TINYPODS_REPOSITORY_TIMEOUT_SECONDS` bound work.

`TINYPODS_REPOSITORY_CREDENTIALS` optionally supplies a host-keyed JSON mapping:

```json
{"registry.example.com":{"username":"ACCOUNT","password":"ACCESS_TOKEN","token_host":"auth.example.com"}}
```

When supplied, credentials are used immediately; otherwise requests use public
access. No rate-limit-triggered account switching is implemented. Upstream throttling
returns the distinct source_rate_limited code without an automatic retry. token_host is
needed only when the OCI account token service uses another host: it explicitly
authorizes that destination to receive credentials. Public registries' anonymous
Bearer exchanges require no account configuration. Tokens stay in memory for the
request and credentials are excluded from repository rows, API responses and
settings representations. Keep actual credential configuration outside version
control, like other operator secrets.

Availability and digests are upstream metadata observations. Sync verifies OCI
config-blob digests, but it does not download or verify chart archive contents or
signatures. Future chart consumption must fetch and verify the pinned package
digest again. External deletion/outages can make an old pin unavailable because
TinyPods does not retain a mirror. Operators choose openly licensed sources;
metadata availability alone is not a license determination.

## Client boundary and verification

RepositoryClient composes separate Helm-index and OCI clients over a bounded
transport. RepositorySync owns database transactions; CLI and HTTP share that
service. The Python Helm-wrapper evaluation is recorded in the PR: the reviewed
async wrapper uses shell subprocess execution and does not provide the controlled
metadata transport/discovery boundary required here. No unused Helm installation
or application-lifecycle framework is included.

Controlled tests cover source metadata, optional credentials, redirect/DNS
boundaries, stale/failed sync rollback, immutable pins, cascade protection and
migration preservation. The public Jetstack demonstration is opt-in and uses only
disposable PostgreSQL/Valkey fixtures:

```sh
uv run --no-sync pytest tests/test_repository_live.py -m live_repository -s --no-cov
```


### Initial version window

Newly synced charts import only their latest three distinct stable semantic versions by
default, independently for each chart. Override with
`tinypods repositories sync --initial-versions 5` or
`POST /api/v1/chart-repositories/{id}/sync?initial_versions=5` (1–10000).
This also applies to charts discovered later or registered without versions.
Subsequent syncs retain existing records and import versions at or above the
oldest already imported version, so skipped history is not backfilled. Changing
N does not trim existing history. Digest changes remain separate immutable pins.

The public-network demo is deselected from the default suite. Select its
`live_repository` marker explicitly; the regular CI suite requires zero skips.


### Latest version pointer

Chart-version responses expose the read-only boolean `is_latest`. A successful
sync marks exactly one highest available semantic version per chart, excluding
prereleases. Equal-precedence labels and digests use deterministic tie-breaks.
Charts absent from the snapshot have no latest version. Failed syncs preserve
the previous pointer, and application release pins do not change.
Use `GET /api/v1/chart-versions?chart_id=...&is_latest=true` to find it.
Migration 0005 creates the repository schema and latest-version flag together.
Existing release pins are backfilled as unverified and stay unmarked until sync. A database constraint allows at most one
latest row per chart and requires that row to be available.

Sync imports stable semantic versions only. Alpha, beta, release-candidate and
other prerelease identifiers are skipped before applying the initial N-version
limit. Build metadata (for example `1.2.3+build.1`) remains supported. Previously
pinned prerelease records are retained but become unavailable and cannot be
latest after sync; they do not determine the stable import window.


### Application presentation metadata

Sync retains each version's upstream description and icon URL from the Helm
index or verified OCI chart config. Version API responses expose both fields.
When a release is linked to an application, its selected chart version fills
an empty application description and missing icon. Explicit operator edits are
preserved on later release writes. Sync never invents descriptions or downloads
icon files, and fetching metadata does not create applications automatically.
