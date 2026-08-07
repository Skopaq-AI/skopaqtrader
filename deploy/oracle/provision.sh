#!/usr/bin/env bash
#
# SkopaqTrader — Oracle Cloud Infrastructure provisioning
#
# Creates the Always Free ARM box that runs the whole stack:
#   VCN → internet gateway → route table → security list → subnet
#       → reserved public IP → VM.Standard.A1.Flex (2 OCPU / 12 GB)
#
# Run this in the **OCI Cloud Shell** (Console → top-right terminal icon).
# Cloud Shell ships an already-authenticated `oci` CLI, so there are no keys
# to configure and nothing to install.
#
#   git clone https://github.com/samuelvinay91/skopaqtrader.git
#   cd skopaqtrader/deploy/oracle
#   ./provision.sh
#
# Everything is idempotent: re-running reuses resources it already created
# (matched by display name) instead of duplicating them.  Safe to re-run after
# a failure, and safe to leave retrying for hours while it waits for capacity.
#
# Env overrides:
#   SKOPAQ_OCI_REGION      target region              (default: ap-hyderabad-1)
#   SKOPAQ_OCI_OCPUS       ARM cores                  (default: 2)
#   SKOPAQ_OCI_MEMORY_GB   RAM in GB                  (default: 12)
#   SKOPAQ_OCI_BOOT_GB     boot volume in GB          (default: 100)
#   SKOPAQ_OCI_SSH_KEY     public key path            (default: ~/.ssh/id_rsa.pub)
#   SKOPAQ_OCI_COMPARTMENT compartment OCID           (default: tenancy root)
#   SKOPAQ_OCI_RETRY_MAX   capacity retries           (default: 720 ≈ 12h)
#   SKOPAQ_OCI_RETRY_WAIT  seconds between retries    (default: 60)
#
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────

REGION="${SKOPAQ_OCI_REGION:-ap-hyderabad-1}"
OCPUS="${SKOPAQ_OCI_OCPUS:-2}"
MEMORY_GB="${SKOPAQ_OCI_MEMORY_GB:-12}"
BOOT_GB="${SKOPAQ_OCI_BOOT_GB:-100}"
SSH_KEY_PATH="${SKOPAQ_OCI_SSH_KEY:-$HOME/.ssh/id_rsa.pub}"
RETRY_MAX="${SKOPAQ_OCI_RETRY_MAX:-720}"
RETRY_WAIT="${SKOPAQ_OCI_RETRY_WAIT:-60}"

# Always Free ARM shape.  Oracle cut the free allowance from 4 OCPU / 24 GB to
# 2 OCPU / 12 GB effective 2026-06-15 (enforced 2026-08-18), so the defaults
# above stay inside the free envelope.  Going higher will start billing.
SHAPE="VM.Standard.A1.Flex"

# Ubuntu rather than Oracle Linux: arm64 wheel coverage on PyPI is better
# tested there, and the repo's Docker tooling assumes a Debian-family host.
OS_NAME="Canonical Ubuntu"
OS_VERSION="24.04"

PREFIX="skopaq"
VCN_NAME="${PREFIX}-vcn"
IG_NAME="${PREFIX}-ig"
SL_NAME="${PREFIX}-sl"
SUBNET_NAME="${PREFIX}-subnet"
INSTANCE_NAME="${PREFIX}-trader"
PUBLIC_IP_NAME="${PREFIX}-ip"
VCN_CIDR="10.0.0.0/16"
SUBNET_CIDR="10.0.1.0/24"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLOUD_INIT="${SCRIPT_DIR}/cloud-init.yaml"

# ── Output helpers ──────────────────────────────────────────────────────────

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

command -v oci >/dev/null 2>&1 || die "oci CLI not found. Run this in the OCI Cloud Shell."

if [[ ! -f "$CLOUD_INIT" ]]; then
    die "cloud-init.yaml not found next to this script (looked in ${SCRIPT_DIR})"
fi

if [[ ! -f "$SSH_KEY_PATH" ]]; then
    warn "No SSH public key at ${SSH_KEY_PATH}"
    info "Generating one now (no passphrase, Cloud Shell home is persistent)..."
    ssh-keygen -t rsa -b 4096 -N "" -f "${SSH_KEY_PATH%.pub}" >/dev/null
    ok "Created ${SSH_KEY_PATH}"
fi
SSH_KEY="$(tr -d '\n' < "$SSH_KEY_PATH")"

# Cloud Shell exports OCI_TENANCY; fall back to querying the subscription.
TENANCY="${OCI_TENANCY:-}"
if [[ -z "$TENANCY" ]]; then
    TENANCY="$(oci iam region-subscription list --query 'data[0]."tenancy-id"' --raw-output 2>/dev/null || true)"
fi
[[ -n "$TENANCY" ]] || die "Could not determine tenancy OCID. Set SKOPAQ_OCI_COMPARTMENT explicitly."

COMPARTMENT="${SKOPAQ_OCI_COMPARTMENT:-$TENANCY}"

export OCI_CLI_REGION="$REGION"

info "Region      ${REGION}"
info "Compartment ${COMPARTMENT}"
info "Shape       ${SHAPE} — ${OCPUS} OCPU / ${MEMORY_GB} GB / ${BOOT_GB} GB boot"
echo

# ── Small helpers ───────────────────────────────────────────────────────────

# Find an existing resource by display name. Echoes the OCID or nothing.
# Deliberately filters on lifecycle state so a half-deleted resource from a
# previous run isn't mistaken for a live one.
find_by_name() {
    local kind="$1" name="$2" extra="${3:-}"
    # shellcheck disable=SC2086
    oci $kind list \
        --compartment-id "$COMPARTMENT" \
        $extra \
        --query "data[?\"display-name\"=='${name}' && \"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='TERMINATING'] | [0].id" \
        --raw-output 2>/dev/null || true
}

# ── 1. VCN ──────────────────────────────────────────────────────────────────

info "Network"

VCN_ID="$(find_by_name "network vcn" "$VCN_NAME")"
if [[ -n "$VCN_ID" && "$VCN_ID" != "null" ]]; then
    ok "VCN exists — ${VCN_ID##*.}"
else
    VCN_ID="$(oci network vcn create \
        --compartment-id "$COMPARTMENT" \
        --display-name "$VCN_NAME" \
        --cidr-blocks "[\"${VCN_CIDR}\"]" \
        --dns-label "${PREFIX}vcn" \
        --wait-for-state AVAILABLE \
        --query 'data.id' --raw-output)"
    ok "VCN created — ${VCN_ID##*.}"
fi

# ── 2. Internet gateway ─────────────────────────────────────────────────────

IG_ID="$(find_by_name "network internet-gateway" "$IG_NAME" "--vcn-id $VCN_ID")"
if [[ -n "$IG_ID" && "$IG_ID" != "null" ]]; then
    ok "Internet gateway exists"
else
    IG_ID="$(oci network internet-gateway create \
        --compartment-id "$COMPARTMENT" \
        --vcn-id "$VCN_ID" \
        --display-name "$IG_NAME" \
        --is-enabled true \
        --wait-for-state AVAILABLE \
        --query 'data.id' --raw-output)"
    ok "Internet gateway created"
fi

# ── 3. Default route table → internet gateway ───────────────────────────────

RT_ID="$(oci network vcn get --vcn-id "$VCN_ID" \
    --query 'data."default-route-table-id"' --raw-output)"

oci network route-table update \
    --rt-id "$RT_ID" \
    --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"${IG_ID}\"}]" \
    --force >/dev/null
ok "Default route → internet gateway"

# ── 4. Security list ────────────────────────────────────────────────────────
#
# Ingress is SSH only.  The API deliberately binds to loopback on the box and
# is reached over an SSH tunnel, so there is no public 8000 to attack.  See
# docs/deployment/oracle-cloud.md for opening 443 behind a real TLS proxy once
# you have a domain.

SL_ID="$(oci network vcn get --vcn-id "$VCN_ID" \
    --query 'data."default-security-list-id"' --raw-output)"

oci network security-list update \
    --security-list-id "$SL_ID" \
    --display-name "$SL_NAME" \
    --ingress-security-rules '[
        {"protocol":"6","source":"0.0.0.0/0","isStateless":false,
         "tcpOptions":{"destinationPortRange":{"min":22,"max":22}},
         "description":"SSH"}
    ]' \
    --egress-security-rules '[
        {"protocol":"all","destination":"0.0.0.0/0","isStateless":false,
         "description":"All outbound - broker, LLM APIs, Supabase, HF"}
    ]' \
    --force >/dev/null
ok "Security list — ingress 22/tcp only, egress open"

# ── 5. Subnet ───────────────────────────────────────────────────────────────

SUBNET_ID="$(find_by_name "network subnet" "$SUBNET_NAME" "--vcn-id $VCN_ID")"
if [[ -n "$SUBNET_ID" && "$SUBNET_ID" != "null" ]]; then
    ok "Subnet exists"
else
    SUBNET_ID="$(oci network subnet create \
        --compartment-id "$COMPARTMENT" \
        --vcn-id "$VCN_ID" \
        --display-name "$SUBNET_NAME" \
        --cidr-block "$SUBNET_CIDR" \
        --dns-label "${PREFIX}sub" \
        --route-table-id "$RT_ID" \
        --security-list-ids "[\"${SL_ID}\"]" \
        --wait-for-state AVAILABLE \
        --query 'data.id' --raw-output)"
    ok "Subnet created"
fi
echo

# ── 6. Image lookup ─────────────────────────────────────────────────────────
#
# Image OCIDs are region-specific and rotate as Canonical publishes new builds,
# so resolve the newest matching one at run time rather than pinning a literal.

info "Image"

IMAGE_ID="$(oci compute image list \
    --compartment-id "$COMPARTMENT" \
    --operating-system "$OS_NAME" \
    --operating-system-version "$OS_VERSION" \
    --shape "$SHAPE" \
    --sort-by TIMECREATED --sort-order DESC \
    --query 'data[0].id' --raw-output 2>/dev/null || true)"

if [[ -z "$IMAGE_ID" || "$IMAGE_ID" == "null" ]]; then
    die "No ${OS_NAME} ${OS_VERSION} aarch64 image found for ${SHAPE} in ${REGION}.
     List what is available with:
       oci compute image list --compartment-id ${COMPARTMENT} --shape ${SHAPE} --output table"
fi
ok "${OS_NAME} ${OS_VERSION} (aarch64) — ${IMAGE_ID##*.}"
echo

# ── 7. Launch, retrying only on genuine capacity errors ─────────────────────

info "Instance"

EXISTING="$(find_by_name "compute instance" "$INSTANCE_NAME")"
if [[ -n "$EXISTING" && "$EXISTING" != "null" ]]; then
    ok "Instance already exists — ${EXISTING##*.}"
    INSTANCE_ID="$EXISTING"
else
    # --raw-output only unquotes scalars; a list still comes back as JSON, so
    # parse it properly rather than stripping punctuation with tr. python3 is
    # guaranteed present — the oci CLI is itself a Python program.
    mapfile -t ADS < <(oci iam availability-domain list \
        --compartment-id "$COMPARTMENT" \
        --query 'data[].name' \
        | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)))')

    [[ ${#ADS[@]} -gt 0 ]] || die "Could not list availability domains in ${REGION}"
    info "${#ADS[@]} availability domain(s): ${ADS[*]}"

    USER_DATA="$(base64 -w0 < "$CLOUD_INIT" 2>/dev/null || base64 < "$CLOUD_INIT" | tr -d '\n')"

    METADATA="$(printf '{"ssh_authorized_keys":"%s","user_data":"%s"}' "$SSH_KEY" "$USER_DATA")"
    SHAPE_CONFIG="$(printf '{"ocpus":%s,"memoryInGBs":%s}' "$OCPUS" "$MEMORY_GB")"

    INSTANCE_ID=""
    attempt=0

    while (( attempt < RETRY_MAX )); do
        for AD in "${ADS[@]}"; do
            attempt=$(( attempt + 1 ))
            printf '  attempt %d/%d  %s ... ' "$attempt" "$RETRY_MAX" "$AD"

            set +e
            OUT="$(oci compute instance launch \
                --compartment-id "$COMPARTMENT" \
                --availability-domain "$AD" \
                --display-name "$INSTANCE_NAME" \
                --shape "$SHAPE" \
                --shape-config "$SHAPE_CONFIG" \
                --image-id "$IMAGE_ID" \
                --subnet-id "$SUBNET_ID" \
                --boot-volume-size-in-gbs "$BOOT_GB" \
                --assign-public-ip false \
                --metadata "$METADATA" \
                --wait-for-state RUNNING 2>&1)"
            RC=$?
            set -e

            if (( RC == 0 )); then
                INSTANCE_ID="$(printf '%s' "$OUT" | grep -o 'ocid1\.instance\.[a-zA-Z0-9._-]*' | head -1)"
                printf '%sgot it%s\n' "$GRN" "$RST"
                break 2
            fi

            # Classify the failure.  Retrying through a config error would spin
            # for twelve hours and teach you nothing, so only capacity and
            # throttling are treated as transient.
            if grep -qiE 'out of host capacity|outofcapacity|internalerror' <<<"$OUT"; then
                printf 'no capacity\n'
            elif grep -qiE 'toomanyrequests|429' <<<"$OUT"; then
                printf 'rate limited\n'
            elif grep -qiE 'limitexceeded|quota' <<<"$OUT"; then
                echo
                die "Service limit hit — this is a quota problem, not capacity.
     You have likely already used your Always Free ARM allowance
     (2 OCPU / 12 GB total across all A1 instances).
     Check: Console → Governance → Limits, Quotas and Usage → Compute → A1.

$OUT"
            else
                echo
                die "Launch failed for a non-transient reason:

$OUT"
            fi
        done
        sleep "$RETRY_WAIT"
    done

    [[ -n "$INSTANCE_ID" ]] || die "Gave up after ${RETRY_MAX} attempts.
     Free-tier A1 capacity in ${REGION} is genuinely contended — this is normal.
     Re-run and leave it going; it will grab one when a slot frees up."

    ok "Instance running — ${INSTANCE_ID##*.}"
fi
echo

# ── 8. Reserved public IP ───────────────────────────────────────────────────
#
# Reserved rather than ephemeral: an ephemeral IP is released when the instance
# is terminated, and this address gets whitelisted with the INDstocks broker.
# Losing it means a support round-trip to re-whitelist.

info "Public IP"

VNIC_ID="$(oci compute instance list-vnics \
    --instance-id "$INSTANCE_ID" \
    --query 'data[0].id' --raw-output)"

PRIVATE_IP_ID="$(oci network private-ip list \
    --vnic-id "$VNIC_ID" \
    --query 'data[?"is-primary"] | [0].id' --raw-output)"

PUBLIC_IP_ID="$(oci network public-ip list \
    --compartment-id "$COMPARTMENT" \
    --scope REGION --lifetime RESERVED \
    --query "data[?\"display-name\"=='${PUBLIC_IP_NAME}'] | [0].id" \
    --raw-output 2>/dev/null || true)"

if [[ -z "$PUBLIC_IP_ID" || "$PUBLIC_IP_ID" == "null" ]]; then
    PUBLIC_IP_ID="$(oci network public-ip create \
        --compartment-id "$COMPARTMENT" \
        --display-name "$PUBLIC_IP_NAME" \
        --lifetime RESERVED \
        --query 'data.id' --raw-output)"
    ok "Reserved public IP created"
else
    ok "Reserved public IP exists"
fi

# Idempotent: assigning an already-assigned IP to the same VNIC is a no-op
# failure, so tolerate it rather than aborting a re-run.
set +e
oci network public-ip update \
    --public-ip-id "$PUBLIC_IP_ID" \
    --private-ip-id "$PRIVATE_IP_ID" \
    --force >/dev/null 2>&1
set -e

PUBLIC_IP="$(oci network public-ip get \
    --public-ip-id "$PUBLIC_IP_ID" \
    --query 'data."ip-address"' --raw-output)"
ok "Assigned — ${BOLD}${PUBLIC_IP}${RST}"
echo

# ── Done ────────────────────────────────────────────────────────────────────

cat <<EOF
${GRN}${BOLD}Provisioned.${RST}

  Instance   ${INSTANCE_ID}
  Public IP  ${BOLD}${PUBLIC_IP}${RST}
  SSH        ssh ubuntu@${PUBLIC_IP}

cloud-init is still installing Docker in the background — give it 2-3 minutes
before the first SSH, and watch it finish with:

  ssh ubuntu@${PUBLIC_IP} 'cloud-init status --wait'

Next steps:

  1. Whitelist ${BOLD}${PUBLIC_IP}${RST} with INDstocks. This is a reserved IP,
     so it survives instance termination.

  2. Copy your secrets over (never commit .env):
       scp .env ubuntu@${PUBLIC_IP}:/opt/skopaq/.env

  3. Bring the stack up:
       ssh ubuntu@${PUBLIC_IP} 'cd /opt/skopaq/skopaqtrader && ./deploy/oracle/bootstrap.sh'

Full walkthrough: docs/deployment/oracle-cloud.md
EOF
