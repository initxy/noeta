# noeta-agent platform image (optional packaging — see README.md; the app
# runs fine bare with `uv` + a writable data directory).
#
# Build from the repository root:
#   docker build -f examples/deployment/Dockerfile -t noeta-agent .

# ---- Stage 1: frontend build ------------------------------------------------
FROM node:20-slim AS web
WORKDIR /src/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

# ---- Stage 2: backend -------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# git: knowledge git_repo sync. curl: docker CLI download below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only (no daemon): the sandbox provider shells out to `docker`
# against the socket mounted by docker-compose. Harmless when the sandbox
# stays disabled.
ARG DOCKER_VERSION=27.3.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY apps/noeta-agent/ apps/noeta-agent/
RUN uv sync --frozen --no-dev

# The backend serves the SPA from apps/web/dist when present.
COPY --from=web /src/apps/web/dist apps/web/dist

# Container defaults; every key is overridable at run time (see
# docs/reference/configuration.md).
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data \
    SHARED_DATA_DIR=/data/shared

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "python", "-m", "noeta.agent"]
