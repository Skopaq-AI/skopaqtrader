# Oracle Cloud deploy kit

Provisions and runs the whole SkopaqTrader stack on one OCI Always Free ARM
instance (2 OCPU / 12 GB), replacing the two Fly.io apps and the Railway daemon
cron.

Full walkthrough: **[docs/deployment/oracle-cloud.md](../../docs/deployment/oracle-cloud.md)**

## Quick start

In the **OCI Cloud Shell** (Console → terminal icon — it has an authenticated
`oci` CLI, nothing to install):

```bash
git clone https://github.com/samuelvinay91/skopaqtrader.git
cd skopaqtrader/deploy/oracle
./provision.sh
```

Then, once it prints the public IP:

```bash
scp .env ubuntu@<ip>:/opt/skopaq/.env
ssh ubuntu@<ip> 'cd /opt/skopaq/skopaqtrader && ./deploy/oracle/bootstrap.sh'
```

## Files

| File | Runs where | Purpose |
|---|---|---|
| `provision.sh` | Cloud Shell | VCN, subnet, security list, reserved IP, A1 instance. Idempotent, retries on capacity errors only. |
| `cloud-init.yaml` | Instance, first boot | Docker CE, `Asia/Kolkata`, swap, repo clone, log caps. Starts nothing. |
| `bootstrap.sh` | Instance, by hand | Validates secrets, builds the image, installs and starts the systemd units. |
| `docker-compose.oci.yml` | Instance | `api` (loopback), `telegram`, plus profile-gated `daemon` and `monitor`. |
| `systemd/skopaq.service` | Instance | Brings the compose stack up at boot. |
| `systemd/skopaq-daemon.service` | Instance | One trading session, one shot. |
| `systemd/skopaq-daemon.timer` | Instance | Mon–Fri 09:10 IST. |

## Notes

- Ingress is **SSH only**. The API binds to `127.0.0.1:8000`; reach it with
  `ssh -L 8000:localhost:8000 ubuntu@<ip>`.
- The public IP is **reserved**, not ephemeral, because it gets whitelisted
  with INDstocks and must survive instance termination.
- Oracle cut the free ARM allowance to 2 OCPU / 12 GB in June 2026 (enforced
  18 Aug 2026) with no announcement. The defaults here stay inside that.
