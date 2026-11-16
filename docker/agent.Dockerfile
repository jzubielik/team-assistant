# syntax=docker/dockerfile:1
# The agent image that AgentCore Runtime runs: linux/arm64 (the Runtime's architecture), the
# locked dependencies, the package and its settings file. Built by `make deploy` (CDK builds
# and pushes it); the build context is the repository root filtered by .dockerignore.
# On an x86-64 host the build runs under QEMU emulation: `make arm64` once per boot.

# Base images: literal tag@sha256 (not ARGs) so Dependabot can bump them.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# The package (installed as a link to /app/src) and the settings it reads from /app/config.
COPY src ./src
COPY config ./config
# The second command adds the user the agent runs as: not root, and with no write access to
# its own code.
RUN uv sync --frozen --no-dev \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
USER 10001

# The Runtime calls POST /invocations and GET /ping on this port.
EXPOSE 8080
# opentelemetry-instrument (aws-opentelemetry-distro) configures the export of spans before the
# agent starts; what is exported is decided by the Runtime's environment variables (stack.py).
CMD ["/app/.venv/bin/opentelemetry-instrument", "/app/.venv/bin/python", "-m", "team_assistant.app"]
