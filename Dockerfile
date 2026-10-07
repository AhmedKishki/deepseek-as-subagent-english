# MCP server for deepseek-as-subagent
# Used by Glama (https://glama.ai/mcp/servers) for automated introspection checks.
# Local users should still use install.sh — this image runs the bare MCP server
# without skill / slash-command / Claude registration.

FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

WORKDIR /app

# Cache layer: copy metadata first so dependency installs are skipped when only code changes
COPY pyproject.toml ./
COPY requirements.lock ./
COPY README.md ./
COPY LICENSE ./
COPY src ./src

# Verify every third-party distribution against the reviewed cross-platform
# lock, then install this source tree without dependency resolution or an
# isolated build environment.
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -m pip check \
    && install -d -o 65532 -g 65532 -m 0700 /home/deepseek

# Keep the package/runtime root-owned and run introspection without root.
ENV HOME=/home/deepseek \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532

# The MCP server uses stdio, so no port is exposed
# Without DEEPSEEK_API_KEY, ping returns NOT_CONFIGURED instead of crashing — suited to Glama
# introspection (startup + tool listing only, no actual delegation)
ENTRYPOINT ["deepseek-mcp"]
