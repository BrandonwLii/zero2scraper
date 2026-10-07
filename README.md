# story-watch

This service watches the Instagram stories of one or more accounts. When a target posts a new story item, it sends a Discord webhook message. It runs as a systemd service in an unprivileged Debian 12 LXC on Proxmox. It only makes outbound connections and has no open ports.

> **Risks:** scraping stories breaks Instagram's terms of service. Use a **burner** account, because a logged-in account that polls every few minutes may be checkpointed or banned. Instagram changes its internals, so expect to run `pip install -U instaloader` (re-running `install.sh` does this for you).

## How it works

Every 300–600 s (random), the service fetches each target's current story items. It compares them with SQLite, notifies Discord about any new items, and marks an item seen only after Discord returns 2xx.

- **First check of a target:** the items already up are recorded silently, without notifications.
- **Instagram errors:** the wait doubles after each failure, up to a 1 h cap.
- **Expired session or checkpoint:** one "re-login" alert is sent.
- **Other repeated failures:** one alert is sent, then one "recovered" message when checks succeed again.

## Setup

1. **Create the LXC** on the Proxmox host. Change the CT ID, storage and bridge to match your setup:
   ```bash
   pveam update && pveam available | grep debian-12
   pveam download local debian-12-standard_12.7-1_amd64.tar.zst
   pct create 120 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
     --hostname story-watch --unprivileged 1 --cores 1 --memory 512 --swap 256 \
     --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
     --onboot 1 --features nesting=1
   pct start 120
   pct exec 120 -- timedatectl set-timezone America/Toronto   # for HEARTBEAT_HOUR
   ```
2. **Install.** Pick one:
   - From your workstation: `PVE_HOST=root@<proxmox-tailscale-name> CTID=120 ./deploy/push.sh`
   - Inside the LXC (`pct enter 120`): get the repo there, then run `bash deploy/install.sh`
3. **Log in to the burner account once**, inside the LXC:
   `sudo -u storywatch /opt/story-watch/.venv/bin/instaloader --login=<burner>`
4. **Create a Discord webhook:** Server Settings → Integrations → Webhooks → New Webhook → Copy URL.
5. **Fill in `/opt/story-watch/.env`** with `IG_USER`, `DISCORD_WEBHOOK` and `TARGETS`. Then send a test message:
   `cd /opt/story-watch && sudo -u storywatch .venv/bin/story-watch --test-notify`
6. **Start the service:** `systemctl enable --now story-watch`
7. **Watch the logs:** `journalctl -u story-watch -f`
8. **When you get a re-login alert:** repeat step 3. The service reloads the session file on its next attempt, so you don't need to restart it. If Instagram asks for a checkpoint, first clear it by logging in to the burner account in the Instagram app or website.

To update, run `push.sh` again (or `git pull && bash deploy/install.sh` in the LXC). It reinstalls the code and restarts the service.

## Development

```bash
uv venv && uv pip install -e '.[dev]'    # or python -m venv .venv && pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/story-watch --once             # one real cycle, using ./.env
```

## Layout

| Path | Purpose |
|---|---|
| `story_watch/config.py` | Loads and validates env / `.env` |
| `story_watch/instagram.py` | instaloader session, cached user ID lookup, `fetch_story_items()` |
| `story_watch/store.py` | SQLite tables `seen` and `targets`, plus pruning after 48 h |
| `story_watch/notify.py` | Discord embeds, with retries on 429 (`retry_after`) and 5xx |
| `story_watch/main.py` | Loop, backoff, alerts, heartbeat, SIGTERM handling, CLI |
| `deploy/` | systemd unit, idempotent `install.sh`, `push.sh` to deploy from a workstation |

On the server, `/opt/story-watch` contains `app/` (code, owned by root), `.venv/`, `.env` (mode 0640), `data/state.db` and `.config/instaloader/session-<burner>`.
