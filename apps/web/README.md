# Console foundation

From the repository root, use Node 24.21.x and npm 11 (versions are pinned in
`.node-version` and `package.json`):

```sh
npm ci
npm run dev
npm run check
npm run build
npm start
```

Development/start bind to loopback on port 3000. This slice is a neutral App
Router page, strict TypeScript, Tailwind v4/shadcn-compatible component wiring,
and Orval/TanStack Query generation. Login, catalog screens, API proxy, cookie/CSRF
integration, deployment controls and Compose hosting are separate follow-ups.
No credentials or backend URLs are configured by the scaffold.

## Structure

- `apps/web/src/app`: routes, root layout and the minimal Query provider.
- `apps/web/src/components/atoms`: app-only primitives; `molecules`: small compositions.
- `organisms` and `templates`: reserved atomic-design folders, without speculative components.
- `packages/ui`: shared primitives and `cn`; `packages/theme`: shared CSS tokens.
- `packages/http`: generated request functions, query/mutation hooks, keys/options and models.

The QueryClient belongs to its mounted provider tree, not a module-level server
singleton. TanStack owns caching, deduplication and request state. Nothing fetches
API data until a later feature consumes a generated function or hook. Auth-related
cache clearing, mutation invalidation, optimistic updates, SSR hydration and retry
policies require feature-specific decisions; OpenAPI does not infer them.

## Generated API

```sh
# After backend contract changes, export via the existing backend command first:
uv run --no-sync tinypods-openapi
npm run api:generate
npm run api:check
npm run test:contracts
```

`apps/web/orval.config.ts` reads the one root `openapi.json`; no schema copy is
maintained. Orval emits TanStack Query hooks over native fetch, including write
mutations, underlying request functions, typed cache-update helpers and models in `packages/http`.
Its normal verb selection is preserved. `forceSuccessResponse` makes HTTP failures
reject so TanStack can represent error state; no custom fetch/cache framework is
added. Generated exports can be consumed from `@tinypods/http/client` and models
from `@tinypods/http/models`. Do not edit generated files.

The drift check generates into a disposable directory and compares file names and
contents, including additions/removals, without modifying checked-in output. Contract
tests check every described GET/POST/PUT/PATCH/DELETE hook against the proper
TanStack primitive and exercise generated success, failure and empty-response
handling with a local fetch stub. Type checks compile the full generated surface.
CI runs these checks and a production build from `npm ci`; the existing backend CI
continues to verify FastAPI/OpenAPI freshness. No backend feature changes are part
of this scaffold.
