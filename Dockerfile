# SkopaqTrader: one image for every service (api, telegram, scheduler, daemon, mcp, chat, ...).
# Used by docker-compose.yml (Mac mini / any always-on host), Fly (fly.toml, fly-telegram.toml)
# and Railway (railway.toml, railway-daemon.toml).
#
# Builds natively on linux/arm64 (Apple Silicon) and linux/amd64 with no compiler: every
# dependency ships CPython 3.14 wheels for both. 3.14 is also the Mac host's MCP interpreter
# (.claude/.mcp.json); CI tests 3.11, 3.12 and 3.14.
#
# The always-on stack (docs/deployment/mac-mini.md), any service (docker/entrypoint.sh) or any
# `skopaq` CLI command:
#     docker compose up -d --build
#     docker run --rm --env-file .env skopaqtrader status
#     docker run --rm --env-file .env skopaqtrader halt "why"

ARG PYTHON_IMAGE=python:3.14-slim-trixie
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# Dependencies first: rebuilt only when pyproject.toml changes.
COPY pyproject.toml ./
RUN pip install -e ".[deploy]"

# Application code, then the project itself (its dependencies are already installed).
COPY . .

# Non-root user. State directories are created here and owned by skopaq, so a new named
# volume mounted on /home/skopaq or /data starts out writable (Docker copies the image's
# ownership into it). /app stays root-owned and read-only for the app.
RUN pip install --no-deps -e . \
    && useradd --create-home --uid 1000 --shell /bin/bash skopaq \
    && mkdir -p /data /home/skopaq/.skopaq /home/skopaq/.tradingagents \
    && chown -R skopaq:skopaq /data /home/skopaq \
    && install -m 0755 docker/entrypoint.sh /entrypoint.sh

USER skopaq
# Relative paths (results/, .cache/, the backtest SQLite fallback) resolve here, not in /app.
WORKDIR /home/skopaq

EXPOSE 8000
# No image-level HEALTHCHECK: each compose service defines its own (api: /health; telegram and
# scheduler: heartbeat files). Fly and Railway use their own checks.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
