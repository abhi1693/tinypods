ARG DOCKERHUB_PREFIX=
FROM ${DOCKERHUB_PREFIX}node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS dependencies
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json .npmrc ./
COPY apps/web/package.json apps/web/package.json
COPY packages/http/package.json packages/http/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/theme/package.json packages/theme/package.json
RUN npm ci
FROM dependencies AS source
COPY tsconfig.base.json ./
COPY apps/web apps/web
COPY packages/http packages/http
COPY packages/ui packages/ui
COPY packages/theme packages/theme
FROM source AS development
RUN chown -R node:node /app
USER node
EXPOSE 3000
CMD ["node", "node_modules/next/dist/bin/next", "dev", "apps/web", "--hostname", "0.0.0.0"]
FROM source AS builder
ARG NEXT_PUBLIC_TINYPODS_API_ORIGIN=http://localhost:8000
ENV NEXT_PUBLIC_TINYPODS_API_ORIGIN=$NEXT_PUBLIC_TINYPODS_API_ORIGIN
RUN npm run build
FROM ${DOCKERHUB_PREFIX}node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=builder --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
