# syntax=docker/dockerfile:1
#
# noeta for servers: `noeta worker` (the default command) and `noeta serve`, with the
# postgres and server extras, built from this checkout. Build from the repository root:
#
#   docker build -f deploy/Dockerfile -t noeta .
#
# deploy/compose.yaml runs Postgres, one `noeta serve` and several workers from it;
# docs/guides/docker.md explains both.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# The extras installed, space-separated (`workflow` is the Workflow tool's JavaScript
# engine, mini-racer).
ARG EXTRAS="postgres server workflow"

WORKDIR /src
# What the wheel and the lock need: the package, its metadata, and the workspace's
# other member (tui/, never installed here; uv reads it to use the lock).
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY tui/pyproject.toml tui/pyproject.toml
COPY noeta noeta

ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# The dependencies at their locked versions, then the wheel of this checkout.
RUN uv export --frozen --no-dev --package noeta --no-emit-workspace --no-hashes \
        $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done) -o /tmp/requirements.txt \
 && uv build --wheel --out-dir /tmp/dist \
 && uv venv /opt/noeta \
 && uv pip install --python /opt/noeta/bin/python --no-cache -r /tmp/requirements.txt \
 && uv pip install --python /opt/noeta/bin/python --no-cache --no-deps /tmp/dist/noeta-*.whl

# -------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm

# git: what a coding agent works with; ripgrep: what the model's `Bash` searches reach
# for; bubblewrap: the Linux sandbox, which inside a container starts only with extra
# rights (docs/guides/docker.md), so it stays off by default: the container is the
# boundary. tini reaps the processes commands leave behind.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bubblewrap ca-certificates git openssh-client procps ripgrep tini \
 && rm -rf /var/lib/apt/lists/*

ARG UID=1000
ARG GID=1000
RUN groupadd --gid "$GID" noeta \
 && useradd --uid "$UID" --gid "$GID" --create-home --shell /bin/bash noeta \
 && mkdir -p /workspace /home/noeta/.noeta \
 && chown noeta:noeta /workspace /home/noeta/.noeta \
 && chmod 700 /home/noeta/.noeta

COPY --from=build /opt/noeta /opt/noeta

ENV PATH=/opt/noeta/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    NOETA_HOME=/home/noeta/.noeta

USER noeta
WORKDIR /workspace
EXPOSE 8642

ENTRYPOINT ["tini", "--", "noeta"]
CMD ["worker"]
