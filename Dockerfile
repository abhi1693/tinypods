# Build from the repository root; both base images are multi-architecture indexes.
FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version ./
COPY apps apps
COPY packages packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages --no-dev --no-editable

FROM base AS runtime
RUN groupadd --gid 10001 tinypods && useradd --uid 10001 --gid 10001 --no-create-home tinypods
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations migrations
USER 10001:10001
EXPOSE 8000
CMD ["tinypods-api"]
