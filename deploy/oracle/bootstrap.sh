#!/usr/bin/env bash
#
# SkopaqTrader — bring the stack up on the OCI ARM box.
#
# Run this ON the instance, after provision.sh has created it and you have
# copied your secrets across:
#
#   scp .env ubuntu@<ip>:/opt/skopaq/.env
#   ssh ubuntu@<ip>
#   cd /opt/skopaq/skopaqtrader && ./deploy/oracle/bootstrap.sh
#
# Idempotent — re-run after a `git pull` to rebuild and restart.
#
set -euo pipefail

REPO_DIR="${SKOPAQ_REPO_DIR:-/opt/skopaq/skopaqtrader}"
ENV_FILE="${SKOPAQ_ENV_FILE:-/opt/skopaq/.env}"
COMPOSE_FILE="${REPO_DIR}/deploy/oracle/docker-compose.oci.yml"
SYSTEMD_SRC="${REPO_DIR}/deploy/oracle/systemd"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
    BOLD=""; RED=""; GRN=""; YLW=""; RST=""
fi
info() { printf '%s==>%s %s\n' "$BOLD" "$RST" "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s  !%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%s  ✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# ── Preflight ───────────────────────────────────────────────────────────────

info "Preflight"

[[ -f "$COMPOSE_FILE" ]] || die "Compose file missing: ${COMPOSE_FILE}"

# cloud-init may still be installing Docker if you SSH'd in early.
if ! command -v docker >/dev/null 2>&1; then
    die "docker not installed yet — cloud-init may still be running.
     Check with:  cloud-init status --wait"
fi

# `usermod -aG docker ubuntu` in cloud-init only takes effect in new login
# sessions. If you SSH'd in before it ran, your current shell lacks the group.
if ! docker info >/dev/null 2>&1; then
    die "Cannot talk to the Docker daemon.
     Usually the docker group hasn't applied to this shell yet — log out and
     back in (or run: newgrp docker), then re-run this script."
fi
ok "Docker $(docker --version | awk '{print $3}' | tr -d ,)"

docker compose version >/dev/null 2>&1 || die "docker compose v2 plugin missing"
ok "Compose plugin present"

[[ -f "$ENV_FILE" ]] || die "No secrets file at ${ENV_FILE}
     Copy it from your machine first:
       scp .env ubuntu@<this-host>:${ENV_FILE}"

[[ -s "$ENV_FILE" ]] || die "${ENV_FILE} is empty — did the scp actually land?"

# Fail loudly rather than starting a trading system with a half-filled config.
missing=()
for key in SKOPAQ_INDSTOCKS_TOKEN SKOPAQ_SUPABASE_URL SKOPAQ_SUPABASE_SERVICE_KEY; do
    if ! grep -qE "^${key}=.+" "$ENV_FILE"; then
        missing+=("$key")
    fi
done
if (( ${#missing[@]} > 0 )); then
    die "${ENV_FILE} is missing values for: ${missing[*]}
     Compare against .env.example at the repo root."
fi

chmod 600 "$ENV_FILE"
ok "Secrets present and mode 600"

# Guard the thing that actually costs money.
if grep -qE '^SKOPAQ_TRADING_MODE=live' "$ENV_FILE"; then
    warn "SKOPAQ_TRADING_MODE=live — this box will place REAL orders once up."
fi
echo

# ── Build ───────────────────────────────────────────────────────────────────

info "Build (first run takes 10-20 min on 2 ARM cores — this is normal)"
docker compose -f "$COMPOSE_FILE" build
ok "Image built"
echo

# ── systemd units ───────────────────────────────────────────────────────────

info "systemd"

sudo install -m 644 "${SYSTEMD_SRC}/skopaq.service"        /etc/systemd/system/
sudo install -m 644 "${SYSTEMD_SRC}/skopaq-daemon.service" /etc/systemd/system/
sudo install -m 644 "${SYSTEMD_SRC}/skopaq-daemon.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
ok "Units installed"

sudo systemctl enable --now skopaq.service
ok "skopaq.service enabled (starts the stack on boot)"

sudo systemctl enable --now skopaq-daemon.timer
ok "skopaq-daemon.timer enabled (Mon-Fri 09:10 IST)"
echo

# ── Status ──────────────────────────────────────────────────────────────────

info "Status"
docker compose -f "$COMPOSE_FILE" ps
echo
systemctl list-timers skopaq-daemon.timer --no-pager || true
echo

cat <<EOF
${GRN}${BOLD}Stack is up.${RST}

The API is bound to loopback. Reach it from your machine with a tunnel:

  ssh -L 8000:localhost:8000 ubuntu@\$(hostname -I | awk '{print \$1}')
  curl localhost:8000/health

Useful:
  docker compose -f ${COMPOSE_FILE} logs -f api
  systemctl status skopaq-daemon.timer
  sudo systemctl start skopaq-daemon.service     # run a session now
  journalctl -u skopaq-daemon.service -f

Update after a code change:
  cd ${REPO_DIR} && git pull && ./deploy/oracle/bootstrap.sh
EOF
