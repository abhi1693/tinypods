# Application definitions

The collection contains four tables: `applications`, `categories`,
`application_categories`, and `releases`.
Application and category slugs are unique. Membership is a unique pair with
restrictive foreign keys. Chart versions are unique within an application.
Application metadata can change independently of release definitions.

Releases identify a Helm repository or OCI chart by source location, chart name,
chart version and SHA-256 digest. These are stored references: this module does
not fetch, verify or run charts. Status has three choices: experimental, stable and premium. It is a single
classification, not billing
or access enforcement. Releases remain editable definitions; publication,
installed snapshots and their immutability are outside this iteration.

`configuration_fields` is a JSONB list of embedded definitions, not another table.
Each definition contains label, type, set_by, path, value, choices, install_only,
sensitive and required. `set_by=chart_default` means leave the Helm value alone;
it neither prompts nor generates an override. `install_only=false` describes
install and update use independently of who supplies the value. A path identifies
one dotted Helm value. Choice options are optional. Sensitive/password fields
cannot store a literal value in a shared definition. Other values describe shared
defaults only; never write installation user values or generated secrets here.

SQLAlchemy binds validate definitions (including updates), reject unknown fields,
invalid choices of type/supplier, malformed paths and non-finite JSON values.
Replace a configuration list to persist edits; nested in-place mutation is not
tracked. PostgreSQL additionally checks outer JSON array shape, status/source
choices, nonempty chart identifiers, slugs, digests, uniqueness and foreign keys.
Direct SQL bypasses embedded definition validation, so only trusted database
writers may write definitions. No generic schema, generation or validation
framework is provided.

Application, Category and Release use the existing transactional audit mixin.
Release history links to its application. Arbitrary configuration JSON is omitted
from audit snapshots; definition changes still produce an update event. The
composite-key membership has no independent audit identity and direct membership
writes are not captured by the single-identity audit foundation. No additional
tables, audit identifiers or audit framework changes are introduced here.

The migration follows `0003_audit_records`; that audit baseline must be included
when this work is integrated. Tests use disposable PostgreSQL and exercise
migration round trips, constraints, validation, audit rollback and independence
of mutable application descriptions. No optional billing integration is needed.
