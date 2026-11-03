ARG FRONTEND_APP=web
ARG FRONTEND_PORT=3000
ARG DOCKERHUB_PREFIX=
FROM ${DOCKERHUB_PREFIX}node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS dependencies
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json .npmrc ./
COPY apps/web/package.json apps/web/package.json
COPY apps/admin/package.json apps/admin/package.json
COPY packages/web-auth/package.json packages/web-auth/package.json
COPY packages/http/package.json packages/http/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/theme/package.json packages/theme/package.json
RUN npm ci
FROM dependencies AS source
COPY tsconfig.base.json ./
COPY apps/web apps/web
COPY apps/admin apps/admin
COPY packages/web-auth packages/web-auth
COPY packages/http packages/http
COPY packages/ui packages/ui
COPY packages/theme packages/theme
FROM source AS development
RUN chown -R node:node /app
ARG FRONTEND_APP
ARG FRONTEND_PORT
ENV TINYPODS_FRONTEND_APP=${FRONTEND_APP} PORT=${FRONTEND_PORT}
USER node
EXPOSE ${FRONTEND_PORT}
CMD ["sh", "-c", "exec node node_modules/next/dist/bin/next dev apps/$TINYPODS_FRONTEND_APP --hostname 0.0.0.0"]
FROM source AS builder
ARG FRONTEND_APP
ARG NEXT_PUBLIC_TINYPODS_WEB_ORIGIN
ARG NEXT_PUBLIC_TINYPODS_API_ORIGIN=http://localhost:8000
ENV NEXT_PUBLIC_TINYPODS_API_ORIGIN=$NEXT_PUBLIC_TINYPODS_API_ORIGIN
ENV NEXT_PUBLIC_TINYPODS_WEB_ORIGIN=$NEXT_PUBLIC_TINYPODS_WEB_ORIGIN
RUN npm run build --workspace "@tinypods/$FRONTEND_APP"
FROM ${DOCKERHUB_PREFIX}node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS runtime
ARG FRONTEND_APP
ARG FRONTEND_PORT
WORKDIR /app
ENV TINYPODS_FRONTEND_APP=${FRONTEND_APP}
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=${FRONTEND_PORT}
COPY --from=builder --chown=node:node /app/apps/${FRONTEND_APP}/.next/standalone ./
COPY --from=builder --chown=node:node /app/apps/${FRONTEND_APP}/.next/static ./apps/${FRONTEND_APP}/.next/static
USER node
EXPOSE ${FRONTEND_PORT}
CMD ["sh", "-c", "exec node apps/$TINYPODS_FRONTEND_APP/server.js"]
