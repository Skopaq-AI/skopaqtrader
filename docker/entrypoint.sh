#!/bin/bash
# SkopaqTrader container entrypoint.
#   <service> [args...]   api | telegram | scheduler | chat | mcp | daemon | daemon-live | monitor | scan | status | shell
#   <skopaq command> ...  any `skopaq` CLI command, e.g. halt "reason", resume --yes, token set <tok>, settle, report
#   <program> ...         python, python3, pip, bash, sh, skopaq, chown, env, or an absolute path
# Extra arguments are passed through. Messages go to stderr: for `mcp`, stdout carries only JSON-RPC.
set -e

SERVICE="${1:-api}"
if [ "$#" -gt 0 ]; then shift; fi

log() { echo "SkopaqTrader: $*" >&2; }
cli() { exec python -m skopaq.cli.main "$@"; }

case "$SERVICE" in
    api)         log "starting FastAPI on port ${PORT:-8000}"; cli serve --host 0.0.0.0 --port "${PORT:-8000}" "$@" ;;
    telegram)    log "starting Telegram bot"; exec python -m skopaq.telegram_bot "$@" ;;
    scheduler)   log "starting scheduler (one daemon session per NSE trading day)"; cli schedule "$@" ;;
    chat)        cli chat "$@" ;;
    mcp)         exec python -m skopaq.mcp_server "$@" ;;
    daemon)      log "starting a paper daemon session now"; cli daemon --once --paper "$@" ;;
    daemon-live) log "starting a LIVE daemon session now"; cli daemon --once --live --confirm-live "$@" ;;
    monitor)     cli monitor "$@" ;;
    scan)        cli scan "$@" ;;
    status)      cli status "$@" ;;
    shell)       exec /bin/bash "$@" ;;
    help|-h|--help) sed -n '2,6p' "$0" >&2; cli --help ;;
    python|python3|pip|bash|sh|skopaq|chown|env|/*) exec "$SERVICE" "$@" ;;
    *)           cli "$SERVICE" "$@" ;;
esac
