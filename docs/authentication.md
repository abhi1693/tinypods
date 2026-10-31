# Supported authentication providers

TinyPods owns identities, durable sessions and resource authorization. Built-in
provider adapters support local username/password authentication and Zitadel OIDC.
Provider discovery exposes the enabled login methods through the API. API keys,
organization management and a browser console are not implemented.

## Enable methods explicitly

Default settings enable local login and disable Zitadel. `GET /auth/providers`
is unauthenticated and returns `{ "data": [...] }` descriptors with ID, label,
password/redirect type and same-origin action paths. It never contacts an IdP or
exposes issuer/client configuration, secrets, tokens or a static authorization URL.
An enabled provider remains advertised during an outage; starting it fails closed.
Disabled provider routes are not registered and do not appear in the running
API schema. The offline client contract lists all supported provider operations.
Disabling local login is explicit; external failure never activates local fallback.
Provider configuration is deployment/operator-owned, not a public mutation API.

Run migrations explicitly before serving requests. Bootstrap the owner from the
trusted backend host with `tinypods auth bootstrap` (Compose: `docker compose run
--rm api tinypods auth bootstrap`). It prompts for an ASCII account name and a
hidden, confirmed 15-128 character password. Names normalize to lowercase;
passwords are not trimmed or normalized. Concurrent bootstraps serialize on the
installation row, and later attempts cannot overwrite the owner. No default
credentials, password arguments or bootstrap HTTP endpoint exist. Argon2id uses
argon2-cffi's RFC 9106 low-memory profile; at most two hash jobs per process run
outside the event loop. Account identity and optional local credentials are separate.

## Zitadel setup

Create a **Web** application with Authorization Code and **PKCE** authentication
in Zitadel. Supported baseline: token endpoint auth `none`, PKCE **S256**. This is
the explicit PKCE setup in the [official login guide](https://zitadel.com/docs/guides/integrate/login/oidc/login-users).
Register the exact callback (HTTPS in production) and optional post-logout URL, then configure:

```sh
TINYPODS_ZITADEL_ENABLED=true
TINYPODS_ZITADEL_ISSUER=https://identity.example.org
TINYPODS_ZITADEL_CLIENT_ID=your-client-id
TINYPODS_ZITADEL_TOKEN_AUTH_METHOD=none
TINYPODS_ZITADEL_REDIRECT_URI=https://tinypods.example.org/auth/providers/zitadel/callback
TINYPODS_ZITADEL_POST_LOGOUT_URI=https://tinypods.example.org/
TINYPODS_ZITADEL_ALLOWED_SUBJECTS='["the-explicitly-admitted-subject"]'
# Optional external-only policy, never an outage-triggered switch:
TINYPODS_AUTH_LOCAL_ENABLED=false
```

The issuer must exactly match discovery, without a trailing slash. No insecure
HTTP/TLS-verification switch exists in production. Self-hosted private HTTPS issuers
are supported with a properly trusted certificate. Discovered authorization,
token, JWKS and optional end-session URLs must stay on that issuer's HTTPS origin;
unchecked redirects and environment proxies are disabled. RS256, code flow, S256
and the configured token authentication method must be advertised. For a
confidential Web client configured for Basic, set `client_secret_basic` and load
`TINYPODS_ZITADEL_CLIENT_SECRET` through the operator's secret mechanism. PKCE
remains mandatory. Do not use an SPA's client secret in frontend code.

`TINYPODS_ZITADEL_ORGANIZATION_ID` selects the organization's login policy and
restricts sign-in to that organization. The signed organization claim must match.
`TINYPODS_ZITADEL_IDENTITY_PROVIDER_ID` is optional and requires an organization;
when set, sign-in can go directly to that upstream provider. When omitted, Zitadel
shows its own login page. Both cases remain a redirect flow: the frontend renders
a sign-in button and follows the fresh link returned by the start action. Only
local accounts use the frontend's username/password form. Organization selection
is checked at sign-in; it does not continuously poll membership for existing
sessions. Subject admission and installation-owner rights remain separate.

Empty allowed subjects denies every external identity. An admitted verified
`(issuer, sub)` may create a new non-owner core account with no local password.
Email, names and organization/role claims never link accounts or elevate access.
To link an allowed subject to an existing account (including the existing owner),
run `tinypods auth link PROVIDER SUBJECT USER_ID` (currently `zitadel` is the supported external provider) from a trusted operator shell. Obtain the
existing core user ID from its authenticated session response or operator database
administration. This explicit security-sensitive link cannot reassign an existing
mapping; it is not an unauthenticated registration or email-matching shortcut.

Zitadel project/organization policies remain upstream controls. Enable project
access enforcement/appropriate grants in Zitadel as required for the installation;
merely asserting roles is not equivalent to requiring project access. TinyPods
still applies its explicit subject admission policy and own ownership checks. See
[project controls](https://zitadel.com/docs/guides/manage/console/projects-overview).
No organization administration or automatic provider-admin role mapping is added.

## Browser flow and session boundaries

1. `POST /auth/providers/zitadel/start`, JSON `{ "return_to": "/apps" }`, with
   `X-CSRF-Token: login` and a matching Origin when supplied. A shared Valkey budget
   reserves the attempt. Only bounded local paths of letters/digits/underscore/
   hyphen/slash are accepted; no query, scheme, traversal or encoded destinations.
2. The backend fetches verified discovery, creates random state, nonce and PKCE
   verifier, and persists a five-minute transaction in PostgreSQL. It returns a
   fresh authorization URL and sets only an opaque browser
   binding cookie (HttpOnly, SameSite=Lax, and Secure in production). State is stored as a digest;
   nonce/verifier remain server-side. Multiple tabs share this opaque browser
   binding but have independent single-use challenges and expiry. The binding
   cookie remains until its five-minute TTL so one callback does not break another.
3. The browser navigates to Zitadel. Its query-mode GET callback uses state plus
   the Lax binding cookie, not a cross-site custom CSRF header. The Strict app
   session cookie is deliberately not needed. Duplicate callback parameters and
   mixed code/error responses fail. The transaction is consumed atomically before
   code exchange, including failed exchanges, and binds the exact provider config.
4. Authlib exchanges code+verifier server-side. joserfc verifies the RS256 signature;
   Authlib validates issuer, audience/authorized party, expiration/issued-at, nonce,
   subject and optional access-token hash. Only verified issuer/subject proceeds to
   admission. Client secrets, provider tokens and verifier never enter browser
   cookies/storage. No UserInfo or email claims are needed. No offline_access,
   refresh tokens or provider API access is requested or retained.
5. The shared session service issues fresh random session and CSRF secrets,
   rotates any prior app session captured at start, then returns 303 to the stored
   local path. Auth responses have no-store and no-referrer headers, and request
   logs omit code/state/query values. Failures are generic and do not activate an
   alternative method. Metadata/JWKS waits and total protocol operations are
   bounded; response size checks are after buffering, not a streaming memory cap
   against a malicious trusted provider.

Local login is `POST /auth/login` with JSON credentials and `X-CSRF-Token: login`.
It uses the same rotated session issuance. In production, every app session cookie uses
Secure, HttpOnly, SameSite=Strict, Path=/ and a `__Host-` name; the separate readable
CSRF cookie is Secure/Strict. Send its value in X-CSRF-Token for mutations.
Production requires HTTPS. Development HTTP uses the cookie policy described below. Configure reverse-proxy
scheme/host forwarding only from trusted proxies so Origin checks reflect the
public API origin. Do not enable wildcard credentialed CORS.

PostgreSQL stores only digests of the random session/CSRF secrets. Absolute session
expiry is 12 hours using database time. Every use reloads live session/account
state; configured API validation also checks whether its source provider remains
enabled and, for external sessions, exact current issuer and subject admission.
Removing admission/disabling the provider denies existing affected sessions after
configuration rollout. All replicas must roll out matching settings. Session rows
remain until operator retention cleanup (not implemented here).

`Principal` carries core identity plus opaque credential kind/ID, never raw browser
cookies/passwords. `AuthorizationService` uses an injected validator. The session
validator fails closed for unsupported credential kinds. Future API-key validation
can implement that boundary without changing resource ownership policy; actual
key issuance, storage and routes are not implemented.

## Logout and limitations

`POST /auth/logout` requires the bound CSRF header, revokes the TinyPods session
immediately and clears app cookies, independently of IdP availability. For optional
upstream logout, `POST /auth/providers/zitadel/logout` first revokes locally and
returns a token-free client-ID-based end-session URL. The browser must navigate
there. If discovery fails, it returns a generic failure **with cleared app cookies**;
local revocation is already committed. Discovery descriptors advertise the ordinary
application logout action, so provider failure cannot trap a user in local login.

Zitadel [RP-initiated logout](https://zitadel.com/docs/guides/integrate/login/oidc/logout)
can affect all accounts in that browser. No automatic upstream logout, back-channel
logout endpoint, provider-session polling, global-device revocation or refresh is
claimed. Upstream account deletion alone is not continuously polled; use TinyPods
admission/config removal or local revocation as appropriate. The app session cannot
outlive its own absolute expiry, but is not coupled to provider token expiry.

Local login allows 30 attempts/minute per installation and five per existing
account; endpoint limits allow ten attempts/minute per transport peer, including
malformed requests. OIDC start similarly has installation and per-peer budgets.
Denials include Retry-After; missing/unavailable Valkey rejects, independent of the
optional public cache. Use non-evicting Valkey persistence: data loss resets budgets.

## Provider code organization

Core adapters live under `auth/providers/local/` and `auth/providers/zitadel/`.
The API mirrors those provider packages for provider-specific routes. Shared
provider discovery, session routes, cookies, identity contracts, transactions and
authorization remain outside individual providers. Functions inside each package
use local names such as `start`, `callback` and `logout`; public API paths and
operation IDs remain stable.

## Development over HTTP

`TINYPODS_ENVIRONMENT` defaults to `production`, which requires HTTPS browser redirects and Secure cookies with the `__Host-` prefix. Explicit `development` mode allows HTTP callback and post-logout URLs and uses unprefixed cookies without Secure. HttpOnly, SameSite, CSRF checks and session verification still apply. The Zitadel issuer and upstream endpoints require HTTPS in both modes. Use development mode only on trusted development networks. Cookies from one mode are not accepted in the other; sign in again after switching modes.
