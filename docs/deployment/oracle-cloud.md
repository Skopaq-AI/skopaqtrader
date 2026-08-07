# Oracle Cloud Deployment

SkopaqTrader runs on a single [Oracle Cloud Infrastructure](https://cloud.oracle.com/) Always Free ARM instance, in the `ap-hyderabad-1` region for low latency to Indian exchanges.

This replaces the two Fly.io apps and the Railway daemon cron with one box that costs nothing.

## Why

| | Fly.io (previous) | Oracle Cloud |
|---|---|---|
| API server | `skopaq-trader`, 1 GB | shared instance |
| Telegram bot | `skopaq-telegram`, 512 MB | shared instance |
| Daemon cron | Railway service | systemd timer |
| Total RAM | 1.5 GB across two VMs | **12 GB** |
| Cost | ~$9–10/month | **$0** |

The memory headroom matters beyond the cost saving. A `shared-cpu-1x` Fly machine caps at 2 GB, and `import torch` alone measures 495 MB resident — so the Fly sizing left no room for the Kronos forecasting work without moving to a larger, paid VM.

!!! warning "Free tier terms change without notice"
    Oracle cut the Always Free ARM allowance from 4 OCPU / 24 GB to **2 OCPU / 12 GB** on 15 June 2026, enforced 18 August 2026, [with no announcement or customer notification](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/). Users found out when instances were shut down.

    `provision.sh` defaults to 2 OCPU / 12 GB so you stay inside the current envelope. Treat the free tier as something that can shrink again, and keep [Docker deployment](docker.md) viable as an exit path.

## Architecture

```
OCI ap-hyderabad-1
└── skopaq-vcn (10.0.0.0/16)
    └── skopaq-subnet (10.0.1.0/24)
        └── skopaq-trader  VM.Standard.A1.Flex, 2 OCPU / 12 GB, Ubuntu 24.04 aarch64
            ├── reserved public IP  ← whitelisted with INDstocks
            ├── docker: api       (127.0.0.1:8000)
            ├── docker: telegram
            └── systemd timer     → docker: daemon  (Mon–Fri 09:10 IST)
```

Ingress is **SSH only**. The API binds to loopback and is reached over an SSH tunnel — see [Reaching the API](#reaching-the-api).

## Prerequisites

- An Oracle Cloud account. The home region is fixed at signup and **cannot be changed**, so make sure it's the one you want before provisioning.
- Your `.env` file, populated. See [Configuration](../getting-started/configuration.md).

You do **not** need the `oci` CLI, Terraform, or an SSH key locally. Cloud Shell provides all three.

## Step 1: Provision

Open the **Cloud Shell** from the Console (terminal icon, top right). It has an already-authenticated `oci` CLI, so there is nothing to install or configure.

```bash
git clone https://github.com/samuelvinay91/skopaqtrader.git
cd skopaqtrader/deploy/oracle
./provision.sh
```

This creates the VCN, internet gateway, route table, security list, subnet, reserved public IP, and the instance itself. It is idempotent — re-running reuses whatever already exists rather than duplicating it.

Overrides, if you need them:

```bash
SKOPAQ_OCI_REGION=ap-mumbai-1 \
SKOPAQ_OCI_BOOT_GB=150 \
./provision.sh
```

| Variable | Default | Notes |
|---|---|---|
| `SKOPAQ_OCI_REGION` | `ap-hyderabad-1` | Must be a region you're subscribed to |
| `SKOPAQ_OCI_OCPUS` | `2` | Above 2 leaves the free tier |
| `SKOPAQ_OCI_MEMORY_GB` | `12` | Above 12 leaves the free tier |
| `SKOPAQ_OCI_BOOT_GB` | `100` | 200 GB total block storage is free |
| `SKOPAQ_OCI_RETRY_MAX` | `720` | ≈12 hours at the default interval |
| `SKOPAQ_OCI_RETRY_WAIT` | `60` | Seconds between capacity retries |

### About "Out of host capacity"

Free-tier A1 capacity is genuinely contended, and a failed launch is the normal first experience rather than a sign something is wrong. `provision.sh` retries automatically, rotating through every availability domain in the region.

Crucially, it **only** retries on capacity and throttling errors. A wrong subnet OCID or an incompatible image fails immediately with the actual error, instead of spinning silently for twelve hours — which is the failure mode of the naive `while true; do launch; done` loops you'll find online.

If it exhausts its retries, just run it again and leave it going. Hyderabad is usually less contended than Mumbai.

A `LimitExceeded` error is different and stops immediately: that means quota, not capacity. You've already used your ARM allowance somewhere else in the tenancy. Check **Governance → Limits, Quotas and Usage → Compute → A1**.

## Step 2: Wait for cloud-init

The instance boots and installs Docker in the background. Give it two or three minutes:

```bash
ssh ubuntu@<public-ip> 'cloud-init status --wait'
```

cloud-init sets the timezone to `Asia/Kolkata`, installs Docker CE with the compose v2 plugin, adds 2 GB of swap, clones the repo to `/opt/skopaq/skopaqtrader`, and caps journal and container log growth.

It deliberately does **not** start anything — that needs secrets, and secrets never travel through instance metadata.

## Step 3: Secrets

```bash
scp .env ubuntu@<public-ip>:/opt/skopaq/.env
```

`bootstrap.sh` verifies the file is present, non-empty, contains `SKOPAQ_INDSTOCKS_TOKEN`, `SKOPAQ_SUPABASE_URL`, and `SKOPAQ_SUPABASE_SERVICE_KEY`, and chmods it to `600`. It refuses to start an incompletely configured trading system.

## Step 4: Bring it up

```bash
ssh ubuntu@<public-ip>
cd /opt/skopaq/skopaqtrader && ./deploy/oracle/bootstrap.sh
```

The first build takes **10–20 minutes** on two ARM cores. That's expected, and only happens once — later runs reuse Docker layers.

!!! note "If Docker permission is denied"
    cloud-init adds `ubuntu` to the `docker` group, but group membership only applies to new login sessions. If you SSH'd in while cloud-init was still running, log out and back in, then re-run.

The script builds the image, installs three systemd units, and starts the stack.

## INDstocks IP whitelisting

`provision.sh` allocates a **reserved** public IP rather than an ephemeral one. Reserved IPs survive instance termination; ephemeral ones don't. Since this address gets whitelisted with INDstocks, losing it means a support round-trip.

Whitelist the IP printed at the end of provisioning.

!!! tip "The Cloudflare Tunnel may now be redundant"
    The tunnel existed to give Fly a static IP for the INDstocks whitelist. An OCI instance has a static public IP natively, so outbound broker calls already come from a fixed address. Verify with `curl ifconfig.me` on the box — if it matches the reserved IP, you can drop `SKOPAQ_CF_TUNNEL_ID` and retire the tunnel.

## Reaching the API

The API binds to `127.0.0.1:8000`, and the security list admits only `22/tcp`. Nothing is publicly exposed.

```bash
ssh -L 8000:localhost:8000 ubuntu@<public-ip>
curl localhost:8000/health
```

### Exposing it publicly

Only do this behind TLS. An unauthenticated trading API on plain HTTP is not something to leave on the internet.

You need **three** changes — this trips people up, because OCI enforces at two layers:

1. **Security list** — add ingress `443/tcp` in `provision.sh`.
2. **Host firewall** — OCI Ubuntu images ship a restrictive `iptables` INPUT chain *in addition to* the security list:
   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. **TLS terminator** — put Caddy or nginx in front with a real domain and certificate. Do not simply change the port binding to `0.0.0.0`.

## Operations

```bash
# Service state
docker compose -f deploy/oracle/docker-compose.oci.yml ps
docker compose -f deploy/oracle/docker-compose.oci.yml logs -f api

# Daemon schedule
systemctl list-timers skopaq-daemon.timer
journalctl -u skopaq-daemon.service -f

# Run a session right now
sudo systemctl start skopaq-daemon.service

# Position monitor (only useful with open positions)
docker compose -f deploy/oracle/docker-compose.oci.yml --profile monitor up -d monitor

# Deploy a code change
cd /opt/skopaq/skopaqtrader && git pull && ./deploy/oracle/bootstrap.sh
```

### The daemon schedule

`skopaq-daemon.timer` fires `Mon-Fri 09:10:00` in local time — five minutes before the NSE open, matching what `railway-daemon.toml` did as `40 3 * * 1-5` in UTC. Because cloud-init sets the host to `Asia/Kolkata`, there is no UTC conversion to get wrong.

Two deliberate choices:

- **`Persistent=false`.** A missed run does not fire on next boot. Starting a "pre-open" session at 14:00 against a mid-session market is worse than skipping the day.
- **No `Restart=`.** A session that died halfway through should be investigated, not silently re-run against a market that has moved.

The timer fires on NSE trading holidays too. Holiday handling stays in `skopaq/risk/calendar.py` rather than being duplicated in a systemd unit nobody remembers to update each January.

## Cutting over from Fly

Run both in parallel first. Nothing here touches `fly.toml` or `fly-telegram.toml`.

1. Provision and bring up OCI, in **paper mode** (`SKOPAQ_TRADING_MODE=paper`).
2. Stop the Telegram bot on one side — two pollers on the same bot token will fight over updates:
   ```bash
   fly scale count 0 -a skopaq-telegram
   ```
3. Point `NEXT_PUBLIC_API_URL` in the Vercel frontend at the new API once you've exposed it.
4. Disable the Railway daemon cron so it doesn't run alongside the systemd timer — two daemons on the same broker account will both try to trade.
5. Watch a full session, then switch trading mode.
6. Once you're confident: `fly apps destroy skopaq-trader skopaq-telegram`.

Rollback is `fly scale count 1` plus `sudo systemctl stop skopaq skopaq-daemon.timer`.

## Known constraints

- **ARM builds are slow.** Two cores, and some dependencies compile from source. The compose file pins `PYTHON_VERSION=3.12` rather than the repo default of 3.14, because several packages in the langchain/pandas tree still lack 3.14 aarch64 wheels and fall back to sdists.
- **One region, one instance.** No redundancy. If the box dies, the daemon misses that day. Acceptable for a single-user system; not for anything with an SLA.
- **Free tier is revocable.** See the warning at the top.
- **`ap-hyderabad-1` has a single availability domain.** AD rotation in the retry loop helps in multi-AD regions like Ashburn or Frankfurt, but there is only one to try here — patience is the only lever.

## See also

- [Docker Deployment](docker.md) — the underlying image and service selector
- [Fly.io Deployment](flyio.md) — the previous setup
- [Configuration](../getting-started/configuration.md) — every `SKOPAQ_*` key
