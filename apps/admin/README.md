# TinyPods Admin Portal

TinyPods Admin Portal is a separate Next.js app. For local Compose development, add
`admin` to `COMPOSE_PROFILES`. The service publishes to loopback on port 3001 by
default. Set `TINYPODS_ADMIN_PUBLISH_HOST` and `TINYPODS_ADMIN_PUBLISH_PORT` for
the desired listener.

Add the exact browser-visible admin origin to `TINYPODS_BROWSER_ORIGINS` alongside
any user-web origin that calls the API. Use the same browser-visible hostname
for both local apps and the API so their session cookies work across ports.
Both apps call `NEXT_PUBLIC_TINYPODS_API_ORIGIN` directly. Set
`NEXT_PUBLIC_TINYPODS_WEB_ORIGIN` to show a link back to the user app.

Run `docker compose up --build --watch` with the build overlay enabled to sync
admin source and shared auth, UI, theme, and generated API sources. The
`TINYPODS_ADMIN_BUILD_TARGET` option selects the development or standalone runtime
image. Supply public origins at build time for standalone images.

The API checks the instance-owner policy before the Admin Portal shell renders.
Sign-in methods come from the existing authentication discovery endpoint.
