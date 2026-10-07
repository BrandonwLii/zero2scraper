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

The commands run on three machines: your **workstation** (where this repo is checked out), the **Proxmox host** and the **LXC** (CT 120). The LXC needs no Tailscale, SSH or open ports, because you reach it only through the host with `pct`.

Neither the Proxmox host nor the Debian LXC template includes `sudo`. To run something as the service user from a root shell in the LXC, use `runuser -u storywatch -- <command>`. The `storywatch` user has no password and can't log in.

### 1. Check SSH from the workstation to the host

```bash
ssh root@<proxmox-host> pct list
```

`<proxmox-host>` is the host's Tailscale name (`tailscale status --self` on the host) or its `100.x` address. The command must list containers without asking for a password. If it asks, set up key auth (`ssh-copy-id`) or Tailscale SSH. Check that the CT ID you plan to use (120 here) isn't already in the list.

### 2. Create the LXC (Proxmox host)

Find your storage and bridge names with `pvesm status` and `ip -br link`. Then look up the current Debian 12 template:

```bash
pveam update
pveam available --section system | grep debian-12
```

Older template versions get removed, so use the exact version that this prints, for example `12.12-1`:

```bash
pveam download local debian-12-standard_<version>_amd64.tar.zst
pct create 120 local:vztmpl/debian-12-standard_<version>_amd64.tar.zst \
  --hostname story-watch --unprivileged 1 --cores 1 --memory 512 --swap 256 \
  --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --onboot 1 --features nesting=1
pct start 120
pct exec 120 -- timedatectl set-timezone America/Toronto   # for HEARTBEAT_HOUR
pct exec 120 -- getent hosts deb.debian.org                # check DNS
```

Keep `nesting=1`: the systemd hardening in `deploy/story-watch.service` needs it. Add `,firewall=1` to `--net0` if you use the Proxmox firewall. The service only needs outbound access.

**If DNS fails** (`Temporary failure in name resolution`), right after `pct start` the container may still be getting its address, so wait a few seconds and retry. Next, check `pct exec 120 -- ip -4 addr`, `ip route` and `ping -c1 1.1.1.1`. If the container has an address and can reach `1.1.1.1` but names still don't resolve, the DNS servers it copied from the host aren't answering. For example, the host may use Tailscale's `100.100.100.100`, or an ISP resolver that doesn't answer the container. Set DNS servers in the container's own config (editing `/etc/resolv.conf` inside the container doesn't last, because Proxmox rewrites it at boot):

```bash
pct set 120 --nameserver "<router-ip> 1.1.1.1"
pct reboot 120
```

### 3. Install (workstation)

```bash
PVE_HOST=root@<proxmox-host> CTID=120 ./deploy/push.sh
```

This streams the repo into the LXC and runs `deploy/install.sh`. The install script creates the `storywatch` user, `/opt/story-watch`, the venv, `.env` (from `.env.example`) and the systemd unit. On the first run, it doesn't start the service. Instead it prints the remaining steps. To check that it worked, run `pct exec 120 -- id storywatch` on the host.

If you prefer, you can install from inside the LXC instead: `pct enter 120`, get the repo there, then run `bash deploy/install.sh`.

### 4. Log in to the burner Instagram account

The service needs an instaloader session file at `/opt/story-watch/.config/instaloader/session-<burner>`, owned by `storywatch` with mode `0600`. There are two ways to create it.

**A. Log in directly (LXC).** The prompt must change to `root@story-watch`, not `root@<host>`, before you run `runuser`:

```bash
ssh -t root@<proxmox-host> pct enter 120
runuser -u storywatch -- /opt/story-watch/.venv/bin/instaloader --login=<burner>
```

instaloader prompts for the password, and for a 2FA code if the account has 2FA. A new device or IP often triggers a checkpoint, and instaloader then tells you to "point your browser" to a URL. Approving the login in the Instagram app ("This was me") sometimes clears it, after which you can retry. A `/auth_platform/?apc=...` URL, though, only works inside the login attempt that created it, so opening it in your own browser just redirects to instagram.com. In that case, use method B.

**B. Import a browser session (workstation, then push).** This method skips the checkpoint:

1. Log in to the burner account on instagram.com in **Firefox** and clear any challenge there. Then close Firefox completely so it saves its cookies to disk. Use Firefox because instaloader can't read the cookies of recent Chrome versions.
2. Install the tools in the workstation venv, find the cookie database and import it. On WSL, the Windows Firefox profile is under `/mnt/c/Users/<WinUser>/AppData/Roaming/Mozilla/Firefox/Profiles/`.
   ```bash
   uv pip install -e '.[dev]' browser_cookie3   # or .venv/bin/pip install ...
   ls /mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite
   .venv/bin/instaloader --load-cookies firefox --cookiefile <path-to>/cookies.sqlite
   ```
   The import must print `Logged in as <burner>`. It writes `~/.config/instaloader/session-<burner>`.
3. Copy the session into the LXC with the right owner and mode, and delete the temporary copy on the host. The session file acts like a password, so don't leave copies of it lying around:
   ```bash
   scp ~/.config/instaloader/session-<burner> root@<proxmox-host>:/root/
   ssh root@<proxmox-host> 'pct push 120 /root/session-<burner> \
     /opt/story-watch/.config/instaloader/session-<burner> \
     --user storywatch --group storywatch --perms 0600 \
     && rm /root/session-<burner>'
   ssh root@<proxmox-host> pct exec 120 -- ls -l /opt/story-watch/.config/instaloader/
   ```

### 5. Configure and test (LXC)

To create a Discord webhook, go to Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Then edit `/opt/story-watch/.env` as root, without changing its mode (0640, `root:storywatch`):

```
IG_USER=<burner>
TARGETS=account1,account2
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
```

`TARGETS` ships with an example value, so replace it. Each entry can be `username:userid` (for example `zero2sudo:50350974961`); with the userid the service skips the username lookup, which Instagram rate-limits hard. To find a userid, open the profile in a logged-in browser, view the page source and search for `profile_id`. Keep the webhook URL private, because anyone who has it can post to the channel.

Send a test message:

```bash
cd /opt/story-watch && runuser -u storywatch -- .venv/bin/story-watch --test-notify
```

### 6. Start the service (LXC)

```bash
systemctl enable --now story-watch
journalctl -u story-watch -f
```

The log shows the loaded config and `loaded Instagram session for <burner>`. The first successful check of each target records the stories already up without sending notifications. After that, each check schedules the next one in 300–600 s.

**A `429 Too Many Requests` right after setup** is normal after several logins and checkpoints. The service backs off by itself (the wait doubles, up to 1 h), sends one alert after `FAIL_ALERT_THRESHOLD` failures and sends "recovered" when checks succeed again. **Don't restart it** while it's rate-limited, because each restart sends a request straight away and resets the backoff. It usually clears within a few hours. The 429 on `web_profile_info` comes from the one-time username → ID lookup, which is cached after it succeeds.

To read the logs from the workstation:

```bash
ssh root@<proxmox-host> pct exec 120 -- journalctl -u story-watch -n 100 --no-pager
```

## Re-login

When Discord gets a "re-login" alert (the session expired or Instagram wants a checkpoint), create a new session file with step 4. If a checkpoint stopped method A before, go straight to method B. If Instagram flagged the account itself, clear that first by logging in to the burner account in the app or website. You don't need to restart anything: the service reloads the session file on its next attempt, and a successful check sends "recovered".

## Updating

To deploy a new version, run `PVE_HOST=root@<proxmox-host> CTID=120 ./deploy/push.sh` again, or `git pull && bash deploy/install.sh` inside the LXC. Both reinstall the code, upgrade instaloader and restart the service. They leave `.env`, the database and the session file alone.

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
