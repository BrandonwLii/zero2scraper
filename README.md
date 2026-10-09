# story-watch

This service watches the Instagram stories of one or more accounts. When a target posts a new story item, it sends a Discord webhook message. It runs as a systemd service in an unprivileged Debian 12 LXC on Proxmox. It only makes outbound connections and has no open ports.

Short version: [HUMANS.md](HUMANS.md).

> **Risks:** scraping stories breaks Instagram's terms of service. Use a **burner** account, because a logged-in account that polls every few minutes may be checkpointed or banned. Instagram changes its internals, so expect to run `pip install -U instaloader` (re-running `install.sh` does this for you).

## How it works

Every 300–600 s (random), the service fetches each target's current story items. It compares them with SQLite, notifies Discord about any new items, and marks an item seen only after Discord returns 2xx.

- **First check of a target:** the items already up are recorded silently, without notifications.
- **Instagram errors:** the wait doubles after each failure, up to a 1 h cap.
- **Expired session or checkpoint:** one "re-login" alert is sent.
- **Other repeated failures:** one alert is sent, then one "recovered" message when checks succeed again.

When there are new items, the service also loads the target's story page once (`/stories/<user>/`, about 1 MB). The GraphQL feed has no stickers, but the page embeds link stickers and @mentions. Links are unwrapped from `l.instagram.com` and stripped of `fbclid`/`utm_*`. Loading the page runs no JavaScript, so it doesn't mark stories seen. If it fails, the notification still goes out without links.

For a link that looks like a job posting, the service fetches that page and uses the job name as the embed title, with the hiring company in the header line above it. It takes JSON-LD `JobPosting.title` first, then `og:title` or `<title>`, then a title-like URL slug. If none of those work, the title is `@user: <site>`. Stories without a job link keep "New story from @user". These fetches never send Instagram cookies, are https-only, refuse private/LAN addresses (including after redirects), and read at most 2 MB.

### Post types, filters and role pings

Each new story gets tags (see [docs/tags.md](docs/tags.md)) from the classifier named in `CLASSIFIER`: a post type (`event`, `job_posting`, `process_info` or `misc`) plus sponsorship, company, role and level. For each dimension the classifier returns the set of values it can't rule out, so "unsure" is several values. `rules`, the default, is a placeholder: any link sticker means post type `job_posting`, everything else `misc`, and every other dimension is left unsure. The tags are stored at first classification and reused if delivery is retried. For now the filters and pings below act on the post type only. Both fail open: a story is filtered out only if every post type it could have is off, and it pings the roles of every post type it could have. Then:

- `NOTIFY_EVENT`, `NOTIFY_JOB_POSTING`, `NOTIFY_PROCESS_INFO` and `NOTIFY_MISC` (default `true`) control whether that type is posted at all. A filtered story is recorded as handled, so it isn't reconsidered every cycle.
- With `PING_ROLES=true`, the message pings the role IDs in `ROLE_EVENT`, `ROLE_JOB_POSTING`, `ROLE_PROCESS_INFO` or `ROLE_MISC` (comma-separated). Only those roles can be mentioned, and nothing else in the message can ping anyone.
- The embed footer shows the tags (a `?` means unsure; dimensions that don't apply to the post type are left out). If the classifier raises an error, every tag is unsure, so the story is still sent and pings every role.
- `interview_info` was renamed `process_info`. `NOTIFY_INTERVIEW_INFO`, `ROLE_INTERVIEW_INFO` and `TEST_ROLE_INTERVIEW_INFO` still work, with a deprecation warning, when the new name isn't set. Rename them when convenient.

### One Discord server

The project supports exactly one Discord server: `DISCORD_WEBHOOK` and its `ROLE_*` pings. A story is marked seen only after that webhook accepts it. If the post fails, the story is retried next cycle (without re-posting the ones that already went through), and the failure counts toward the "Story watcher failing" alert. `DISCORD_WEBHOOK_<n>` and `ROLE_*_<n>` (earlier multi-server settings) are ignored: the service logs a warning naming each one that is still set.

To add a classifier (for example an LLM), implement `classify(item) -> Tags` and register it in `CLASSIFIERS` in `story_watch/classify.py`.

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

This streams the repo, plus your local `.env` without its `TEST_*` lines, into the LXC and runs `deploy/install.sh`. The install script creates the `storywatch` user, `/opt/story-watch`, the venv, `.env` and the systemd unit. `.env` is your workstation's copy if you have one, otherwise `.env.example`. On the first run, it doesn't start the service. Instead it prints the remaining steps. To check that it worked, run `pct exec 120 -- id storywatch` on the host.

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

To create a Discord webhook, go to Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Then put these in your workstation's `./.env` and run `push.sh` again, which installs it as `/opt/story-watch/.env` (mode 0640, `root:storywatch`). If you installed from inside the LXC instead, edit that file as root, keeping its mode:

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

To deploy a new version, run `PVE_HOST=root@<proxmox-host> CTID=120 ./deploy/push.sh` again, or `git pull && bash deploy/install.sh` inside the LXC. Both reinstall the code, upgrade instaloader and restart the service. `push.sh` also replaces `/opt/story-watch/.env` with your local `.env` (minus `TEST_*`) and keeps the previous one as `.env.bak`, so make config changes locally, not on the server. Before shipping, `push.sh` runs `story-watch --check-config` on that file and stops if the service would reject it (for example a malformed `DISCORD_WEBHOOK`; leftover `DISCORD_WEBHOOK_<n>` or `ROLE_*_<n>` only produce a warning). Once the service has stayed up for 5 seconds after the restart, `push.sh` posts "story-watch deployed" with the commit id to the Discord webhook (`-dirty` means uncommitted changes were deployed), followed by a changelog: only the commit subjects since the previous deploy, up to 15 (none if the previous deploy isn't known). `push.sh` ships the last 100 commits of `git log`, and `install.sh` records each deployed commit in `/opt/story-watch/deployed-commit` to know where the last deploy was. You can send it by hand with `story-watch --deploy-notify <commit>`. Neither touches the database or the session file, and `install.sh` run inside the LXC leaves `.env` alone.

## Story archive

Stories expire after 24 hours and their image URLs expire with them, so the service can keep its own copy for labeling and evaluating taggers. It is off unless `ARCHIVE_DIR` is set.

To enable it on the server, put this in your local `.env` and deploy:

```
ARCHIVE_DIR=/opt/story-watch/archive
ARCHIVE_MAX_MB=2048        # optional total cap, default 2048
```

`install.sh` creates `/opt/story-watch/archive` (owned by `storywatch`, mode 0700) and the systemd unit lets the service write there. For every new story, including ones that `NOTIFY_*` filters out, the watcher saves into `<ARCHIVE_DIR>/<target>/`:

- `<taken_at>_<media id>.jpg`: the full-resolution still image (for a video story, its poster frame; videos are never saved; images are capped at 25 MB). Older archives may still hold `.mp4` files from before; they are left alone.
- `<taken_at>_<media id>.json`: the sidecar with media id, target, `taken_at`, `is_video`, links, mentions, job title, company, the classifier name, its `tags` (`Tags.to_dict()`) and the old single `category` (kept for older readers), the saved file names, `download_error` (an error type, or null), and the raw Instagram item `node` and `page_node` (with stickers).

Archiving never delays or blocks a post. The copy is made in the same cycle that finds the story, after all accounts' posts have been sent (or skipped, or have failed and will be retried), so a slow download can't delay a post. Any error is logged by type only and skipped. Downloads use their own HTTP session with no Instagram cookies, https only, public addresses only, no redirects and the size caps above. The first run after you enable it only seeds the current stories and archives nothing; only stories that appear afterwards are saved. When the archive passes `ARCHIVE_MAX_MB`, the oldest items are deleted, so pull it regularly. The sidecars contain signed image URLs and the story media, so never commit them; a repo-local `archive/` is gitignored.

To copy the archive to the workstation (it tars it inside the CT over the same `ssh` + `pct exec` path as the deploy, and can be re-run any time):

```bash
PVE_HOST=root@<proxmox-host> CTID=120 .venv/bin/python scripts/pull_archive.py
```

It writes to `~/story-watch-data/archive/` by default, outside the repo and shared by all worktrees; set `ARCHIVE_PULL_DIR` to change that (`ARCHIVE_REMOTE_DIR` changes the path inside the CT). Files the server has since deleted stay on the workstation.

## Labeling bot

`story-watch-bot` is a Discord bot that runs as a second systemd unit (`story-watch-bot.service`) next to the watcher. Its first feature is labeling: it posts every archived story in a private channel so people can tag it, which builds the ground-truth set for evaluating taggers (#6). It is one bot for one Discord server; story notifications still go out through the webhooks and the bot never changes that. Setup (application, invite, ids) is in `HUMANS.md`.

| Variable | Meaning |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token. Never logged or put in error messages. `install.sh` only enables the unit when it is set |
| `LABEL_CHANNEL_ID` | The private channel stories are posted in; buttons work only there |
| `LABELER_USER_IDS` | Comma-separated Discord user ids allowed to label. Anyone else gets a private refusal |
| `ARCHIVE_DIR` | The archive the watcher writes (required) |
| `LABELS_FILE` | Optional. Absolute path for the labels (default `<ARCHIVE_DIR>/labels.jsonl`) |
| `LABEL_POLL_SECONDS` | Optional. Seconds between archive scans (default 30) |
| `LABEL_BACKLOG_MAX` | Optional. On the very first start only the newest N stories are posted (default 10); the rest are skipped for good |
| `LABEL_SINCE` | Optional. Ignore stories older than this ISO date |

**How labeling works.** Every 30 seconds the bot looks for archived stories it hasn't posted yet and posts them oldest first: the image (or, for an old archive item that has an `.mp4`, the video when it fits the server's upload limit), the links, mentions, job title and company, and the classifier's current guess. It remembers what it posted in `<ARCHIVE_DIR>/label-posts.jsonl`, so a restart doesn't repost. Press **Label** to open a form with one multi-select per tag dimension (post type, sponsorship, company, role, level; pick several for a story that covers several jobs). The post type's guess is pre-selected. The form saves nothing: it opens a private preview where you check the choices, optionally add a note, and press **Save**. Dimensions that don't apply to the chosen post type (`APPLICABLE` in `story_watch/tags.py`) are saved as not applicable; the others need at least one value. After saving, the post shows the labels and who saved them, and the button becomes **Relabel**; the last saved label for a story wins. Only users in `LABELER_USER_IDS` can label.

**Label file.** `labels.jsonl` gets one JSON line per save (history is kept):

```json
{"media_id": "...", "target": "...", "post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["quant", "other"], "role": ["swe", "pm"], "level": ["internship"], "labeler": "<Discord user id>", "note": "", "labeled_at": "2026-01-01T12:00:00+00:00", "taxonomy": "tags-1a2b3c4d"}
```

Each dimension is a list of the stable value strings from `docs/tags.md` in enum order, or `null` when it doesn't apply to the post type. `taxonomy` is derived from the dimensions, values and `APPLICABLE`, so it changes whenever the taxonomy does. The file and `label-posts.jsonl` sit at the top level of the archive, not in the per-target folders, because the archive's size cap deletes files in those folders. `scripts/pull_archive.py` copies the whole archive directory, so the labels come along (and replace the local copy of those two files).

The unit restarts at most 5 times an hour, 2 minutes apart, and not at all after a config or token error (exit code 2), because Discord resets a bot token after 1000 gateway logins in 24 hours. Check the environment with `.venv/bin/story-watch-bot --check-config`; logs: `journalctl -u story-watch-bot`.

## Development

```bash
uv venv && uv pip install -e '.[dev]'    # or python -m venv .venv && pip install -e '.[dev]'
.venv/bin/pre-commit install             # once per clone: secret scan on every commit
.venv/bin/pytest
.venv/bin/story-watch --once             # one real cycle, using ./.env
```

The pre-commit hook runs [gitleaks](https://github.com/gitleaks/gitleaks) with its default rules plus rules for Discord webhook URLs and Instagram `sessionid` cookies (`.gitleaks.toml`), and refuses `.env`, `session-*`, `*.har`, `*.db`, `story-dump*` and `story-cache/` even when they are force-added. The repo is public, so don't skip it with `--no-verify`. If it flags a test fixture, make the fixture look less real rather than adding an allowlist. `.venv/bin/pre-commit run --all-files` scans the whole tree.

### Testing embeds with `scripts/resend_story.py`

This script resends one of the target's current stories to a separate Discord webhook, so you can change the embed, job lookup or classifier and see the result without waiting for a new story. It runs on the workstation with the same burner session file and `.env` as `--once`, and never touches the service database.

Set these in `./.env` (the service ignores them):

```
TEST_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...   # a test channel
TEST_ROLE_JOB_POSTING=<role id>      # optional: roles to ping in the test server
TEST_ROLE_EVENT=
TEST_ROLE_PROCESS_INFO=
TEST_ROLE_MISC=
```

```bash
.venv/bin/python scripts/resend_story.py --list            # numbered stories, 1 = most recent
.venv/bin/python scripts/resend_story.py -n 3              # send the 3rd most recent
.venv/bin/python scripts/resend_story.py -n 1-5,8          # several at once
.venv/bin/python scripts/resend_story.py -n 3 --dry-run    # print the Discord payload, send nothing
.venv/bin/python scripts/resend_story.py -n 3 --no-ping    # don't ping the TEST_ROLE_* roles
.venv/bin/python scripts/resend_story.py --refresh -n 1    # re-fetch from Instagram first
```

- **Instagram is only contacted on the first run or with `--refresh`.** The raw stories are cached in `story-cache/<target>.json` (gitignored). Every run rebuilds the embed from that cache with the current code, so edits to `notify.py`, `jobs.py` or `classify.py` show up on the next run. Job pages are fetched live each time.
- **Thumbnail URLs in the cache expire after about a day.** Use `--refresh` when images stop loading.
- **Every selected story is sent, even if `NOTIFY_*` would filter it.** The output says when the service would skip it.
- **Pings come only from `TEST_ROLE_*`.** `ROLE_*` and `PING_ROLES` are ignored, so a test can't ping production roles.
- **For each story, it prints the post type, company, job title, links and roles to the terminal.** The webhook URL is never printed.

## Layout

| Path | Purpose |
|---|---|
| `story_watch/config.py` | Loads and validates env / `.env` |
| `story_watch/instagram.py` | instaloader session, cached user ID lookup, `fetch_story_items()`, story-page links and mentions (`with_extras()`) |
| `story_watch/classify.py` | The `Classifier` interface and the rule-based classifier |
| `docs/tags.md` | Draft story tag taxonomy, labeling guide and classifier output contract (`story_watch/tags.py`, not wired in yet) |
| `story_watch/jobs.py` | Job-title lookup for link stickers (JSON-LD / og:title / slug), with SSRF guards |
| `scripts/dump_story.py` | Diagnostic: dump raw GraphQL and story-page JSON for a target |
| `scripts/resend_story.py` | Re-send the Nth most recent story to `TEST_DISCORD_WEBHOOK`, from a local cache, to iterate on embeds |
| `story_watch/archive.py` | Saves each new story's media and a JSON sidecar when `ARCHIVE_DIR` is set (size-capped, no cookies) |
| `scripts/pull_archive.py` | Pull the archive from the CT to `~/story-watch-data/archive/` over ssh + `pct exec` |
| `scripts/seed_test_archive.py` | Write fake stories (sidecar + placeholder PNG) to `~/story-watch-data/test-archive/` for trying the bot locally |
| `story_watch/store.py` | SQLite tables `seen` and `targets`, plus pruning after 48 h |
| `story_watch/notify.py` | Discord embeds, with retries on 429 (`retry_after`) and 5xx |
| `story_watch/main.py` | Loop, backoff, alerts, heartbeat, SIGTERM handling, CLI |
| `story_watch/bot/` | Discord bot (`story-watch-bot`): `labels.py` (label format and validation), `queue.py` (archive scan, posted log), `render.py`, `config.py`, `discord_app.py` (discord.py glue) |
| `deploy/` | systemd units, idempotent `install.sh`, `push.sh` to deploy from a workstation |

On the server, `/opt/story-watch` contains `app/` (code, owned by root), `.venv/`, `.env` (mode 0640), `data/state.db`, `archive/` (see below) and `.config/instaloader/session-<burner>`.
