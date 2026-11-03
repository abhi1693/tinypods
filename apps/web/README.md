# Web login

The web app uses shared atomic UI, Orval-generated Fetch/TanStack Query and
Zod validators. The centered login layout follows the supplied /dev/push UX
reference with TinyPods branding and theme colors.

```sh
npm ci
npm run dev
npm run check
npm run build
npm run format
npm run format:check
```

Use Node 24.21.x and npm 11. Python stays on Ruff; Prettier handles supported
TS/TSX/JS/JSON/YAML/CSS/Markdown, including generated TypeScript. The canonical
openapi.json is excluded because the backend exporter owns its exact formatting.

## Authentication and direct API configuration

Browsers call the API directly. There are no Next API forwarding routes,
rewrites, proxy or duplicate endpoint client. Configure the browser-visible
API origin and exact trusted frontend origin in the Docker host's .env:

```dotenv
COMPOSE_PROFILES=local,web
TINYPODS_ENVIRONMENT=development
TINYPODS_WEB_PUBLISH_HOST=192.168.1.101
NEXT_PUBLIC_TINYPODS_API_ORIGIN=http://192.168.1.101:8000
TINYPODS_BROWSER_ORIGINS=["http://192.168.1.101:3000"]
```

Substitute the actual private host address. API and frontend use the same hostname
with different ports; localhost in a remote browser refers to that browser's
machine. Repository defaults stay on loopback for web publication.
NEXT_PUBLIC_TINYPODS_API_ORIGIN is public configuration, never a secret.
Next substitutes it during production build; build a runtime image for each
API origin. The build overlay passes the same value as a build argument and
development environment setting. OpenAPI's server template is authoritative;
Orval explicitly selects server index 0 and its origin variable.

Backend CORS uses explicit origins with credentials, JSON and CSRF headers.
Origin validation trusts only the API's own origin or this configured list;
wildcards/paths/credentials are rejected, and production origins require HTTPS.
Production HttpOnly/Secure/Strict session cookies remain unchanged. HTTP LAN
review uses the existing development cookie policy. CORS does not make Strict
cookies usable across unrelated sites. Other future authenticated writes also
need the current CSRF token; different-hostname CSRF delivery requires an explicit
backend contract rather than a proxy or weaker cookie attributes.

Local login uses generated credentials/header defaults and a generated LoginRequest
Zod schema. The app maps validation issues to accessible shared fields. Failed
requests clear the password, retries are disabled and mutation retention is zero.
No session or password is written to localStorage. A generated session query
distinguishes expected anonymous 401 from service/network failure; successful
login updates its typed identity cache. Reload verifies the backend cookie.

Provider discovery determines whether local login and external options appear.
Configured external choices use the generated Zitadel start mutation, then open
the backend-issued authorization URL. State, nonce, PKCE, correlation cookies,
token verification and session issuance stay in the backend. Callback destinations
may be a simple relative path or an absolute simple path on an explicitly trusted
browser origin; changed origin configuration invalidates outstanding attempts.
No external provider is fabricated or enabled by the UI.

Current success behavior is an inline account confirmation. Social sign-in requests
the frontend root. Protected-route guards, original deep-link/query capture and
post-login deep-link navigation are not implemented by this slice.
Signup/reset-password, catalog/deployment screens and a new auth framework are
outside this change.

## Shared structure and generated files

- packages/ui/components/atoms: input/button primitives.
- packages/ui/components/molecules: labeled field and provider buttons.
- packages/ui/components/organisms/login-form: reusable presentational form.
- packages/ui/components/templates/auth-shell: centered auth layout.
- apps/web/components/organisms: generated API/schema/session/provider orchestration.
- packages/http/src/generated/client/endpoints: one operation module per tag.
- client/schemas/models: component DTOs/enums; client/schemas/types: operation types.
- client/mocks: generated MSW handlers/default factories, test-only imports.
- generated/validation/models: reusable Zod schemas.

Use @tinypods/http/client, /models, /types and /validation exports. A model that
is a TypeScript type is not duplicated as an invented class. Do not edit generated
files. Regenerate from the canonical root contract:

```sh
uv run --no-sync tinypods-openapi
npm run api:generate
npm run api:check
```

Orval's native Prettier formatter handles each output. Drift checks generate every
output under an ignored repository-local temporary root, so the same formatter
configuration resolves, compare all filenames/contents and delete only that
temporary tree. CI checks formatting, deterministic generation, endpoint
query/mutation contracts, Zod bounds, generated MSW auth flows, workspace types
and production build in the existing .github/workflows/ci.yml.

MSW tests seed generated factories with fixed assertion-critical identity/date
overrides and isolated QueryClients. They cover success/session cache, 401/429
without credential retry, pending/network error and provider initiation.
They do not prove browser cookie/CORS behavior; the real LAN browser check does.

## Docker Compose and Watch

With the existing COMPOSE_FILE selection and configured .env:

```sh
# Full-stack development/Watch:
docker compose up --build --watch

# Frontend only, rebuilding after configuration/dependency changes:
docker compose -f compose.yaml -f compose.build.yaml up --build --force-recreate --watch web

# Production standalone runtime, environment-specific API origin:
TINYPODS_WEB_BUILD_TARGET=runtime docker compose -f compose.yaml -f compose.build.yaml up --build --force-recreate -d --wait web

# Stop only the frontend:
docker compose stop web
```

Without COMPOSE_FILE, pass compose.yaml, compose.build.yaml and
compose.workers.yaml explicitly for full-stack development, retaining any local
key-file overlay already configured. COMPOSE_PROFILES must include web for
commands without explicit service selection. Restart a Watch process after
changing profiles/environment; its old process retains its original selection.
Recreate API/migrate/worker as well when changing backend browser-origin settings.

The normal development host is http://192.168.1.101:3000. Next binds 0.0.0.0 inside
the non-root container, published on the configured private LAN interface.
The configured host also feeds Next allowedDevOrigins for LAN HMR/hydration.

Watch syncs app and shared UI/theme/generated HTTP source. Manifest, lockfile,
TypeScript/Next/PostCSS config and Docker changes rebuild with npm ci against
all workspace manifests. Use --build when starting Watch after it was stopped.
Host node_modules/.next and operator environment files are not synced.
Production copies only traced Next server dependencies and static assets.
