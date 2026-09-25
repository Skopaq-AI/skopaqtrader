#!/usr/bin/env bash
# Mac mini readiness checks for the always-on stack (docs/deployment/mac-mini.md).
#
#   scripts/macmini/verify.sh [--build] [--up] [--probe-kill-switch [--force]]
#                             [--unit-tests] [--dry-run-daemon]
#
# Prints one line per check (PASS | WARN | FAIL | INFO) and exits 1 if any check FAILs.
# Never prints secret values: keys in .env are only checked for presence.
# Runs on macOS's bash 3.2 with BSD tools; all date/time logic runs inside the containers.
set -u
cd "$(dirname "$0")/../.." || exit 1

BUILD=0 UP=0 PROBE=0 FORCE=0 UNIT=0 DRYRUN=0
usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }
for arg in "$@"; do
    case "$arg" in
        --build) BUILD=1 ;;
        --up) UP=1 ;;
        --probe-kill-switch) PROBE=1 ;;
        --force) FORCE=1 ;;
        --unit-tests) UNIT=1 ;;
        --dry-run-daemon) DRYRUN=1 ;;
        -h|--help)
            usage
            cat <<'EOF'

  --build              docker compose build first
  --up                 docker compose up -d first (api, telegram, scheduler)
  --probe-kill-switch  halt in api, check the scheduler sees it, resume (refused on a
                       trading day 09:00-15:45 IST unless --force)
  --unit-tests         run tests/unit inside the image (without .env or the stack's volumes)
  --dry-run-daemon     docker compose run --rm daemon --dry-run (makes scanner LLM calls)
EOF
            exit 0 ;;
        *) echo "unknown option: $arg (see -h)" >&2; exit 2 ;;
    esac
done

PASS_N=0 WARN_N=0 FAIL_N=0
report() {
    printf '%s  %s: %s\n' "$1" "$2" "$3"
    case "$1" in
        PASS) PASS_N=$((PASS_N + 1)) ;;
        WARN) WARN_N=$((WARN_N + 1)) ;;
        FAIL) FAIL_N=$((FAIL_N + 1)) ;;
    esac
}
pass() { report PASS "$1" "$2"; }
warn() { report WARN "$1" "$2"; }
fail() { report FAIL "$1" "$2"; }
info() { report INFO "$1" "$2"; }
indent() { sed 's/^/        /'; }

summary() {
    echo
    echo "Summary: $PASS_N passed, $WARN_N warnings, $FAIL_N failed"
    if [ "$FAIL_N" -gt 0 ]; then exit 1; fi
    exit 0
}

# .env helpers: presence only for secrets; values only for the non-secret keys below.
has_key() { grep -Eq "^$1=[\"']?[^[:space:]\"']" .env 2>/dev/null; }
env_val() {
    sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1 | tr -d '\r' \
        | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

in_svc() { local service="$1"; shift; docker compose exec -T "$service" "$@"; }

# ── Host ──────────────────────────────────────────────────────────────────────
echo "== Host"
OS="$(uname -s)" ARCH="$(uname -m)"
if [ "$OS" = Darwin ] && [ "$ARCH" = arm64 ]; then
    pass "host" "macOS on Apple Silicon ($ARCH)"
else
    warn "host" "$OS/$ARCH (the runbook targets a Mac mini: Darwin/arm64)"
fi

if ! command -v docker >/dev/null 2>&1; then
    fail "docker" "docker CLI not found: install Docker Desktop or OrbStack"
    summary
fi
if docker compose version >/dev/null 2>&1; then
    pass "docker compose" "$(docker compose version --short 2>/dev/null || echo v2)"
else
    fail "docker compose" "Compose v2 (docker compose) not available"
    summary
fi
if ! docker info >/dev/null 2>&1; then
    fail "docker engine" "not reachable: start Docker Desktop/OrbStack (and set it to start at login)"
    summary
fi
pass "docker engine" "server $(docker info --format '{{.ServerVersion}}' 2>/dev/null)"

ENGINE_ARCH="$(docker info --format '{{.Architecture}}' 2>/dev/null)"
case "$ENGINE_ARCH" in
    aarch64|arm64) pass "engine architecture" "$ENGINE_ARCH (native arm64 images)" ;;
    *) warn "engine architecture" "$ENGINE_ARCH (arm64 expected on a Mac mini; other arches emulate)" ;;
esac

MEM="$(docker info --format '{{.MemTotal}}' 2>/dev/null)"
case "$MEM" in ''|*[!0-9]*) MEM=0 ;; esac
MEM_GIB=$((MEM / 1073741824))
if [ "$MEM" -ge $((6 * 1073741824)) ]; then
    pass "engine memory" "${MEM_GIB} GiB"
else
    warn "engine memory" "${MEM_GIB} GiB (give Docker 6-8 GB in its settings)"
fi

if [ "$OS" = Darwin ]; then
    PM="$(pmset -g 2>/dev/null)"
    SLEEP="$(printf '%s\n' "$PM" | awk '$1 == "sleep" {print $2; exit}')"
    AUTORESTART="$(printf '%s\n' "$PM" | awk '$1 == "autorestart" {print $2; exit}')"
    if [ "$SLEEP" = 0 ] && [ "$AUTORESTART" = 1 ]; then
        pass "power" "sleep 0, autorestart 1"
    else
        warn "power" "sleep=${SLEEP:-?} autorestart=${AUTORESTART:-?}: run sudo pmset -a sleep 0 disksleep 0 displaysleep 10 powernap 0 womp 1 autorestart 1 tcpkeepalive 1"
    fi
    AUTOLOGIN="$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)"
    if [ -n "$AUTOLOGIN" ]; then
        pass "automatic login" "$AUTOLOGIN"
    else
        warn "automatic login" "off: after a power cut Docker waits for someone to log in (needs FileVault off)"
    fi
    info "FileVault" "$(fdesetup status 2>/dev/null | head -n 1)"
    info "Docker app" "make sure Docker Desktop/OrbStack starts at login and automatic updates are off"
fi

FREE_KB="$(df -Pk . | awk 'NR == 2 {print $4}')"
case "$FREE_KB" in ''|*[!0-9]*) FREE_KB=0 ;; esac
if [ "$FREE_KB" -ge $((20 * 1024 * 1024)) ]; then
    pass "disk" "$((FREE_KB / 1024 / 1024)) GB free"
else
    warn "disk" "$((FREE_KB / 1024 / 1024)) GB free (20 GB or more recommended)"
fi

# ── Configuration ─────────────────────────────────────────────────────────────
echo
echo "== Configuration"
if [ ! -f .env ]; then
    fail ".env" "missing: cp .env.example .env and fill it in"
    summary
fi
pass ".env" "present"

if has_key SKOPAQ_GOOGLE_API_KEY || has_key GOOGLE_API_KEY; then
    pass "LLM key" "Gemini key set"
else
    fail "LLM key" "set SKOPAQ_GOOGLE_API_KEY (most agents run on Gemini)"
fi
if has_key SKOPAQ_SUPABASE_URL && has_key SKOPAQ_SUPABASE_SERVICE_KEY; then
    pass "Supabase" "URL and service key set"
else
    warn "Supabase" "SKOPAQ_SUPABASE_URL / SKOPAQ_SUPABASE_SERVICE_KEY not set: no shared kill switch, P&L history or memory"
fi
if has_key SKOPAQ_TELEGRAM_BOT_TOKEN && has_key SKOPAQ_TELEGRAM_CHAT_ID; then
    pass "Telegram" "bot token and chat id set"
else
    warn "Telegram" "SKOPAQ_TELEGRAM_BOT_TOKEN / SKOPAQ_TELEGRAM_CHAT_ID not set: no bot, no scheduler alerts"
fi

TRADING_MODE="$(env_val SKOPAQ_TRADING_MODE)"
SCHED_MODE="$(env_val SKOPAQ_SCHEDULER_MODE)"
if [ "${TRADING_MODE:-paper}" = live ] || [ "${SCHED_MODE:-paper}" = live ]; then
    warn "modes" "trading=${TRADING_MODE:-paper} scheduler=${SCHED_MODE:-paper}: LIVE means real orders"
else
    pass "modes" "trading=${TRADING_MODE:-paper} scheduler=${SCHED_MODE:-paper}"
fi
CORS="$(env_val SKOPAQ_CORS_ORIGINS)"
if ! grep -q '^SKOPAQ_CORS_ORIGINS=' .env || [ "$CORS" = "*" ]; then
    warn "CORS" "SKOPAQ_CORS_ORIGINS is '*' (or unset, which means '*'): set your dashboard origin(s)"
else
    pass "CORS" "${CORS:-none (no browser origins)}"
fi

if docker compose config -q >/dev/null 2>&1; then
    SERVICES=" $(docker compose config --services 2>/dev/null | tr '\n' ' ')"
    missing=""
    for svc in api telegram scheduler; do
        case "$SERVICES" in *" $svc "*) ;; *) missing="$missing $svc" ;; esac
    done
    if [ -z "$missing" ]; then
        pass "compose file" "valid; default services:$SERVICES"
    else
        fail "compose file" "default services lack:$missing"
    fi
else
    fail "compose file" "docker compose config failed (run it with -q to see why)"
fi

# ── Stack ─────────────────────────────────────────────────────────────────────
echo
echo "== Stack"
if [ "$BUILD" = 1 ]; then
    info "build" "docker compose build"
    docker compose build || fail "build" "docker compose build failed"
fi
if [ "$UP" = 1 ]; then
    info "up" "docker compose up -d"
    docker compose up -d || fail "up" "docker compose up -d failed"
fi

# Waits for a service to turn healthy (up to 36 x 5 s); prints the last state.
wait_healthy() {
    cid="$(docker compose ps -q "$1" 2>/dev/null)"
    if [ -z "$cid" ]; then echo "not running"; return 1; fi
    i=0
    while [ "$i" -lt 36 ]; do
        state="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$cid" 2>/dev/null)"
        case "$state" in
            running/healthy) echo "$state"; return 0 ;;
            running/*) ;;
            *) echo "$state"; return 1 ;;
        esac
        i=$((i + 1))
        sleep 5
    done
    echo "$state"
    return 1
}

STACK_UP=1
for svc in api telegram scheduler; do
    if state="$(wait_healthy "$svc")"; then
        pass "$svc" "$state"
    else
        fail "$svc" "$state (start with --up; see docker compose logs $svc)"
        if [ "$svc" = api ]; then STACK_UP=0; fi
    fi
done

if [ "$STACK_UP" = 0 ]; then
    info "stack" "api is not running: skipping the in-container checks"
else
    PORT_MAP="$(docker compose port api 8000 2>/dev/null)"
    case "$PORT_MAP" in
        127.0.0.1:*) pass "api port" "$PORT_MAP (loopback only)" ;;
        *) fail "api port" "${PORT_MAP:-not published}: expected 127.0.0.1:8000" ;;
    esac
    if curl -fsS -m 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
        pass "api /health" "http://127.0.0.1:8000/health answers"
    else
        fail "api /health" "http://127.0.0.1:8000/health does not answer"
    fi
    if [ "$OS" = Darwin ]; then
        LAN="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
    else
        LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    if [ -n "$LAN" ]; then
        if curl -fsS -m 3 "http://$LAN:8000/health" >/dev/null 2>&1; then
            fail "api exposure" "reachable on the LAN at $LAN:8000: publish it on 127.0.0.1 only"
        else
            pass "api exposure" "not reachable on the LAN ($LAN:8000)"
        fi
    fi

    CHECK="$(in_svc api python -c '
import os, pathlib, time
print("uid=%d" % os.getuid())
print("tz=%s" % time.strftime("%Z"))
bad = [str(p) for p in (pathlib.Path(d).expanduser() for d in ("~/.skopaq", "~/.tradingagents", "/data", "."))
       if not os.access(p, os.W_OK)]
print("unwritable=%s" % ",".join(bad))
' 2>&1)"
    UID_IN="$(printf '%s\n' "$CHECK" | sed -n 's/^uid=//p')"
    TZ_IN="$(printf '%s\n' "$CHECK" | sed -n 's/^tz=//p')"
    BAD_IN="$(printf '%s\n' "$CHECK" | sed -n 's/^unwritable=//p')"
    if [ -n "$UID_IN" ] && [ "$UID_IN" != 0 ]; then pass "container user" "uid $UID_IN"; else fail "container user" "uid ${UID_IN:-?} (root or unknown)"; fi
    if [ "$TZ_IN" = IST ]; then pass "container time zone" "IST"; else fail "container time zone" "${TZ_IN:-?} (expected IST: TZ=Asia/Kolkata)"; fi
    if [ -n "$UID_IN" ] && [ -z "$BAD_IN" ]; then
        pass "volumes" "home (.skopaq, .tradingagents, working directory) and /data are writable"
    else
        fail "volumes" "not writable: ${BAD_IN:-?}. Fix: docker compose run --rm --no-deps --user root api chown -R skopaq:skopaq /home/skopaq /data"
    fi

    JUNK="$(in_svc api sh -c 'ls /app | grep "^=" || true' 2>/dev/null)"
    if [ -z "$JUNK" ]; then pass "image files" "no stray /app/=* files"; else warn "image files" "stray files in /app: $JUNK"; fi

    GRAPH="$(in_svc api python -c '
from skopaq.cli.main import _build_upstream_config
from skopaq.config import SkopaqConfig
from skopaq.graph.skopaq_graph import SkopaqTradingGraph
SkopaqTradingGraph(_build_upstream_config(SkopaqConfig()), None)._ensure_graph()
print("GRAPH_OK")
' 2>&1)"
    case "$GRAPH" in
        *GRAPH_OK*) pass "analysis graph" "initialises as uid ${UID_IN:-?}" ;;
        *) fail "analysis graph" "$(printf '%s\n' "$GRAPH" | tail -n 1)" ;;
    esac

    if SCHED="$(in_svc scheduler python -m skopaq.cli.main schedule --check 2>&1)"; then
        pass "schedule --check" "ok"
    else
        fail "schedule --check" "exit code non-zero (holiday list or configuration)"
    fi
    printf '%s\n' "$SCHED" | indent

    HALT="$(in_svc api python -c '
from skopaq.execution import kill_switch
print(kill_switch.status(use_cache=False).describe())
' 2>&1 | tail -n 1)"
    case "$HALT" in
        *HALTED*) warn "kill switch" "$HALT" ;;
        "Trading is active") pass "kill switch" "$HALT" ;;
        *) fail "kill switch" "status unreadable: $HALT" ;;
    esac
    FLAGS="$(in_svc api python -c '
from skopaq.execution import kill_switch as k
flags = k._flags(k._config())
if flags is None:
    print("NOT_CONFIGURED")
else:
    flags.get(k.HALT_FLAG_KEY)
    print("FLAGS_OK")
' 2>&1 | tail -n 1)"
    case "$FLAGS" in
        FLAGS_OK) pass "Supabase system_flags" "readable: the kill switch crosses every process" ;;
        NOT_CONFIGURED) warn "Supabase system_flags" "Supabase not configured: a halt reaches only this host's containers" ;;
        *) warn "Supabase system_flags" "read failed: apply supabase/migrations/003_system_flags.sql" ;;
    esac

    EGRESS="$(in_svc api python -c '
import ipaddress, httpx
resp = httpx.get("https://api.ipify.org", timeout=10)
resp.raise_for_status()
print(ipaddress.IPv4Address(resp.text.strip()))
' 2>/dev/null | tail -n 1)"
    EXPECTED="$(env_val SKOPAQ_EXPECTED_EGRESS_IP)"
    if [ -z "$EGRESS" ]; then
        warn "egress IP" "could not determine the containers' public IPv4"
    elif [ -z "$EXPECTED" ]; then
        warn "egress IP" "$EGRESS (set SKOPAQ_EXPECTED_EGRESS_IP to the IPv4 INDstocks whitelisted)"
    elif [ "$EGRESS" = "$EXPECTED" ]; then
        pass "egress IP" "$EGRESS matches SKOPAQ_EXPECTED_EGRESS_IP"
    else
        fail "egress IP" "$EGRESS, but INDstocks whitelists $EXPECTED: orders will be refused"
    fi

    TOKEN_VALID="$(in_svc api python -c '
import json, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
print(json.load(opener.open("http://127.0.0.1:8000/health", timeout=5)).get("token_valid"))
' 2>/dev/null | tail -n 1)"
    if [ "$TOKEN_VALID" = True ]; then
        pass "INDstocks token" "valid"
    else
        warn "INDstocks token" "not valid: set today's token (docker compose exec api skopaq token set <TOKEN>)"
    fi

    if [ "$(env_val SKOPAQ_OLLAMA_ENABLED)" = true ]; then
        if curl -fsS -m 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
            pass "Ollama (host)" "localhost:11434 answers"
        else
            fail "Ollama (host)" "localhost:11434 does not answer: start Ollama"
        fi
        OLLAMA_IN="$(in_svc api python -c '
import os, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open(os.environ["SKOPAQ_OLLAMA_BASE_URL"] + "/api/tags", timeout=5)
print("OLLAMA_OK")
' 2>&1 | tail -n 1)"
        if [ "$OLLAMA_IN" = OLLAMA_OK ]; then
            pass "Ollama (containers)" "reachable through host.docker.internal"
        else
            fail "Ollama (containers)" "unreachable from containers: $OLLAMA_IN"
        fi
    fi
fi

if docker compose logs --since 10m telegram 2>/dev/null | grep -q Conflict; then
    fail "Telegram poller" "HTTP 409 Conflict in the last 10 min: another bot instance polls the same token"
else
    pass "Telegram poller" "no getUpdates conflicts in the last 10 min"
fi
if command -v fly >/dev/null 2>&1; then
    if fly status -a skopaq-telegram 2>/dev/null | grep -Eq '(^|[[:space:]])started([[:space:]]|$)'; then
        warn "Fly Telegram bot" "skopaq-telegram has started machines: fly scale count 0 -a skopaq-telegram"
    fi
fi
info "Railway" "make sure the Railway daemon cron service (railway-daemon.toml) is disabled"
if has_key SKOPAQ_API_BASE_URL; then
    warn "SKOPAQ_API_BASE_URL" "set in .env: every container reads it and api would fetch the Kite token from itself; set it only in the native MCP server's env block (runbook section 13)"
fi
if [ -f .claude/.mcp.json ] && grep -q 'skopaq.mcp_server' .claude/.mcp.json \
    && ! grep -q 'SKOPAQ_API_BASE_URL' .claude/.mcp.json; then
    info "native MCP" ".claude/.mcp.json sets no SKOPAQ_API_BASE_URL: the native server will not see the Kite session and quotes fall back to INDstocks (runbook section 13)"
fi

# ── Optional checks ───────────────────────────────────────────────────────────
if [ "$PROBE" = 1 ] || [ "$UNIT" = 1 ] || [ "$DRYRUN" = 1 ]; then
    echo
    echo "== Optional"
fi

if [ "$PROBE" = 1 ]; then
    WINDOW="$(in_svc scheduler python -c '
from datetime import time
from skopaq.config import SkopaqConfig
from skopaq.risk import calendar as c
now = c.now_ist()
try:
    busy = c.is_trading_day(now.date(), SkopaqConfig().nse_holidays)
except ValueError:
    busy = True
print("BUSY" if busy and time(9, 0) <= now.time() < time(15, 45) else "QUIET")
' 2>/dev/null | tail -n 1)"
    if [ "$WINDOW" != QUIET ] && [ "$FORCE" != 1 ]; then
        warn "kill-switch probe" "skipped: trading day 09:00-15:45 IST (or unknown); rerun outside market hours or add --force"
    else
        HALT_OUT="$(in_svc api python -m skopaq.cli.main halt 'verify.sh probe' 2>&1)"
        SEEN="$(in_svc scheduler python -c '
from skopaq.execution import kill_switch
print(kill_switch.status(use_cache=False).describe())
' 2>&1 | tail -n 1)"
        case "$SEEN" in
            *HALTED*) pass "kill-switch probe" "the scheduler sees the halt set in api" ;;
            *) fail "kill-switch probe" "the scheduler does not see the halt: $SEEN" ;;
        esac
        case "$HALT_OUT" in
            *supabase:system_flags*) pass "kill-switch probe" "recorded in Supabase" ;;
            *) warn "kill-switch probe" "not recorded in Supabase (only this host is halted)" ;;
        esac
        if in_svc api python -m skopaq.cli.main resume --yes >/dev/null 2>&1; then
            pass "kill-switch probe" "resumed"
        else
            fail "kill-switch probe" "resume failed: run docker compose exec api skopaq resume --yes"
        fi
    fi
fi

if [ "$UNIT" = 1 ]; then
    # A bare container of the compose image, not `docker compose run`: a compose service
    # loads .env (production Supabase, Telegram, trading mode) and mounts the production
    # volumes, and tests that halt or notify would then reach them. No env file, no named
    # volumes, and a throwaway HOME inside the container (pip --user lands there, removed
    # with the container). tests/conftest.py also blanks those variables.
    if ! docker image inspect skopaqtrader:local >/dev/null 2>&1; then
        fail "unit tests" "image skopaqtrader:local not built: rerun with --build"
    elif docker run --rm --pull never -e HOME=/tmp/unit-tests -e SKOPAQ_TRADING_MODE=paper \
        -v "$PWD/tests:/app/tests:ro" -w /app skopaqtrader:local shell -c \
        'pip install --user -q pytest pytest-asyncio respx && python -m pytest tests/unit -q -p no:cacheprovider'; then
        pass "unit tests" "pass inside the image (no .env, no volumes)"
    else
        fail "unit tests" "failures inside the image (output above)"
    fi
fi

if [ "$DRYRUN" = 1 ]; then
    if docker compose run --rm daemon --dry-run; then
        pass "daemon dry run" "scan-only session exited 0"
    else
        fail "daemon dry run" "non-zero exit (output above)"
    fi
fi

summary
