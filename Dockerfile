# One image recipe for every workspace member:
#   docker build --build-arg PACKAGE=opspilot-agent .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
ARG PACKAGE

# Dependencies first (cached until a lockfile or pyproject changes).
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/agent/pyproject.toml apps/agent/
COPY apps/knowledge/pyproject.toml apps/knowledge/
COPY apps/erp-mock/pyproject.toml apps/erp-mock/
COPY apps/docflow/pyproject.toml apps/docflow/
RUN uv sync --frozen --no-dev --package "$PACKAGE" --no-install-workspace

COPY packages ./packages
COPY apps ./apps
RUN uv sync --frozen --no-dev --package "$PACKAGE"
