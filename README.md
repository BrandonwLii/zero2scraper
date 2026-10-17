# story-watch

This service watches the Instagram stories of one or more accounts. When a target posts a new story item, it sends a Discord webhook message. It runs as a systemd service in an unprivileged Debian 12 LXC on Proxmox. It only makes outbound connections and has no open ports.

Short version: [HUMANS.md](HUMANS.md).

> **Risks:** scraping stories breaks Instagram's terms of service. Use a **burner** account, because a logged-in account that polls every few minutes may be checkpointed or banned. Instagram changes its internals, so expect to run `pip install -U instaloader` (re-running `install.sh` does this for you).

## How it works

Every 1200–2400 s (random; `MIN_WAIT`/`MAX_WAIT`), the service fetches each target's current story items. It compares them with SQLite, notifies Discord about any new items, and marks an item seen only after Discord returns 2xx.

- **First check of a target:** the items already up are recorded silently, without notifications.
- **Quiet hours:** with `QUIET_HOURS=01:00-07:00` (`HH:MM-HH:MM`, may wrap midnight; blank = off) and `QUIET_TZ` (IANA zone, blank = system local time) the service makes no Instagram requests inside the window, error retries included. It logs `quiet hours until HH:MM` once, then sleeps until the window ends plus the usual random jitter. The heartbeat still goes out, and quiet hours never raise an alert. A bad value is a config error.
- **Instagram errors:** the wait doubles after each failure, up to a 1 h cap.
- **Expired session or checkpoint:** one "re-login" alert is sent.
- **Other repeated failures:** one alert is sent, then one "recovered" message when checks succeed again.

When there are new items, the service also loads the target's story page once (`/stories/<user>/`, about 1 MB). The GraphQL feed has no stickers, but the page embeds link stickers and @mentions. Links are unwrapped from `l.instagram.com` and stripped of `fbclid`/`utm_*`. Loading the page runs no JavaScript, so it doesn't mark stories seen by itself. If it fails, the notification still goes out without links.

**Marking stories seen on Instagram (`MARK_SEEN`, #50, default `false`).** With `MARK_SEEN=true`, after a cycle that found new items and once every post of the cycle has been sent (and the archive saved), the service marks each handled item (posted, or recorded as filtered by `NOTIFY_*` or dropped by the no-post list) seen, oldest first, the way the web app does while showing them: one `PolarisStoriesV3SeenMutation` POST per item to `/api/graphql`, with the same session cookies, the CSRF token from the `csrftoken` cookie, and the `fb_dtsg`/`lsd` tokens of the story page fetched in that cycle (none if that fetch failed, so nothing is marked). Like a viewer, it waits 3–8 s (random) between items, and a cycle spends at most 120 s on it: items not reached by then stay unmarked and are logged as a count. The burner then appears in the target's viewer list. It sends nothing on cycles without new items, never delays or blocks a post, is never retried (a failure is logged, without URLs, cookies or bodies, and those items stay unmarked), and is skipped for the cycle if Instagram already failed in it. A login or checkpoint reply goes through the usual one "re-login" alert and backoff. Items left unposted by a Discord failure are marked after they are posted. The mutation's `doc_id` rotates with Instagram's web bundles, so confirm it against a browser capture before turning this on (HUMANS.md).

For a link that looks like a job posting, the service fetches that page and uses the job name as the embed title, with the hiring company in the header line above it. It takes JSON-LD `JobPosting.title` first, then `og:title` or `<title>`, then a title-like URL slug. If none of those work, the title is `@user: <site>`. Stories without a job link keep "New story from @user". It also reads the posting's text, locations and employment type for the tagger (#9): from the ATS's public API on Workday, Greenhouse, Lever and Ashby, else from the page (JSON-LD, server-rendered app state, the iCIMS iframe, or the visible text), and it notes when a posting has closed. A story whose links are none of them job links gets its first link's page read the same way, for the tagger only (often an event page; #10 scored it with that page). That is at most two requests per story, only in a cycle with new stories, cached per URL for the cycle. These fetches never send Instagram cookies (or any cookies), are https-only, refuse private/LAN addresses on every redirect hop, connect to the address that was checked (so a second DNS answer can't point them at the LAN), and read at most 2 MB. One link's lookup gets at most 30 s in all; after that the story goes out with only a title taken from the URL, if it has one.

### Post types, filters and user pings

Each new story gets tags (see [docs/tags.md](docs/tags.md)) from the classifier named in `CLASSIFIER`: a post type (`event`, `job_posting`, `process_info` or `misc`) plus sponsorship, company, role and level. For each dimension the classifier returns the set of values it can't rule out, so "unsure" is several values. `rules`, the default, is a placeholder: any link sticker means post type `job_posting`, everything else `misc`, and every other dimension is left unsure. `claude` is the production tagger (see [The Claude tagger](#the-claude-tagger-classifierclaude)); `hybrid` is #10's local-model tagger (see [The hybrid tagger](#the-hybrid-tagger-classifierhybrid)). The tags are stored at first classification and reused if delivery is retried. The `NOTIFY_*` filters act on the post type only and fail open: a story is filtered out only if every post type it could have is off. Then:

- `NOTIFY_EVENT`, `NOTIFY_JOB_POSTING`, `NOTIFY_PROCESS_INFO` and `NOTIFY_MISC` (default `true`) control whether that type is posted at all. A filtered story is recorded as handled, so it isn't reconsidered every cycle.
- The server-wide **no-post list** (`/nopost`, #13) drops stories on top of `NOTIFY_*`; see [The no-post list](#the-no-post-list-nopost).
- Each user chooses what they want with `/pings` in the bot ([docs/pings.md](docs/pings.md)). When a story is posted, the service reads everyone's saved preferences, checks them against the story's stored tags, and mentions the matching users at the end of the message. Only those users can be pinged: `allowed_mentions` is `{"parse": [], "users": [...]}`, never roles or everyone. If the preferences can't be read, the story is posted without pings. If the mentions don't fit in one message (100 ids or 2000 characters), the rest go in follow-up messages that contain only mentions; those are best effort, and the story counts as delivered once the main message is accepted. A story is re-posted only when its main message failed, so nobody is pinged twice. Alerts, the heartbeat, `--test-notify`, deploy messages and filtered stories ping nobody.
- The embed footer shows the tags (a `?` means unsure; dimensions that don't apply to the post type are left out). If the classifier raises an error, every tag is unsure, so the story is still sent and pings every user who could match.
- `interview_info` was renamed `process_info`. `NOTIFY_INTERVIEW_INFO` still works, with a deprecation warning, when `NOTIFY_PROCESS_INFO` isn't set. Rename it when convenient.
- Role pings were removed. `PING_ROLES`, `ROLE_<TYPE>` (including `ROLE_INTERVIEW_INFO`) and `TEST_ROLE_<TYPE>` do nothing; if any is still set, the service logs one warning naming them. Delete them from `.env`.

### One Discord server

The project supports exactly one Discord server: `DISCORD_WEBHOOK`, with pings going to individual users. A story is marked seen only after that webhook accepts it. If the post fails, the story is retried next cycle (without re-posting the ones that already went through), and the failure counts toward the "Story watcher failing" alert. `DISCORD_WEBHOOK_<n>` and `ROLE_*_<n>` (earlier multi-server settings) are ignored: the service logs a warning naming each one that is still set.

To add a classifier, implement `classify(item) -> Tags` and register a factory (it gets the `Config`) in `CLASSIFIERS` in `story_watch/classify.py`. A classifier with a model behind it can also have `start_cycle() -> bool`, which the watcher calls before every cycle; it reports whether the model is up. It may also have `down_alert(minutes) -> (title, text)` for the one alert per outage, and `fallback_count` (stories tagged by its fallback), which the heartbeat reports.

### The Claude tagger (`CLASSIFIER=claude`)

Decided on 2026-10-10 after the Claude bake-off (#41): on the 48 labelled stories Claude Opus alone scored 0 missed / 11 extra pings / 2 stories ruling out a true value, against the hybrid's 0 / 26 / 9. Per new story, before it is posted:

1. The still is downloaded (the same guards as the archive).
2. **Claude Opus decides all five dimensions** through headless `claude -p`, on the owner's plan (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`). It gets the image, the link sticker, the mentions and the posting's full text (up to 15,000 characters, from `jobs.py`), under `story_watch/prompts/tag_claude_v1.txt`: the uncertainty contract, the user's decisions and docs/tags.md's definitions, with the company tables rendered from `story_watch/companies.py` (a test keeps them equal to docs/tags.md). The reply must match `story_watch/prompts/tag_claude_v1.schema.json` exactly.
3. **Canada safety net.** If the posting's locations or text, or the locations and titles Claude read, put the job in Canada, `sponsor_or_canadian` is added to the sponsorship. It only ever widens; nothing else is post-processed.
4. **Fallback, fail open.** On a usage limit (a `rate_limit_event` whose status isn't `allowed`/`allowed_warning`), a timeout (`CLAUDE_TIMEOUT`, 60 s; the whole process group is killed), a non-zero exit, a missing or error result, output that breaks the schema, or no still, the story gets the fallback tags: link and posting rules plus keyword rules on RapidOCR text (`listing_rules`; OCR runs only then). If even that raises, every tag is unsure.

The command is `claude -p --model opus --input-format stream-json --output-format stream-json --verbose --json-schema <schema> --system-prompt-file <prompt> --tools '' --safe-mode --strict-mcp-config --setting-sources '' --no-session-persistence`, with the user message (image and text) as one stream-json line on stdin.

- **Sandbox.** No tools (`--json-schema` adds Claude Code's internal `StructuredOutput` tool; the classifier rejects a reply if the init event lists any other tool), no MCP, no settings sources, no session persistence. It runs as `storywatch` with a private HOME (`/opt/story-watch/data/claude-home`, mode 0700), an empty per-call working directory and a minimal environment (no inherited variables; the autoupdater and non-essential traffic are off). The token is only in that environment: never in argv, logs or exception messages.
- **Where story data goes.** Unlike `hybrid`, each story's still and listing text go to Anthropic under the owner's account (decided by the user, 2026-10-10). Nothing else does: no Instagram cookies, no webhook URLs, no user ids.
- **Untrusted input.** The listing and the image are written by third parties. The listing goes between `<<<LISTING` / `LISTING>>>` delimiters (stripped from the text itself) and the prompt says it is data. Claude's reply can only choose tag values: anything outside the schema (unknown values, extra keys, empty sets) is a failure and gets the fallback.
- **Health and alerts.** A health check would cost usage, so Claude counts as down after 2 failed calls in a row, or at once on a usage limit. After 15 minutes down, one "Claude tagger unavailable" alert goes to `DISCORD_WEBHOOK`; "Tagger model recovered" follows after the next story Claude tags. The heartbeat reports how many stories got the fallback tags since the previous heartbeat.
- **Delay.** Claude answers in about 5 s (8.4 s worst in the bake-off). A story waits at most `CLAUDE_TIMEOUT`. After a timeout, a limit or any other failure to answer, Claude is skipped for the rest of the cycle, and a cycle waits on Claude at most 300 s in all (60 stories at the bake-off's speed); the stories after that get the fallback tags straight away. Worst case for a burst: 300 s of Claude plus about 1 s of OCR per remaining story.
- **Usage.** About 0.15–0.3 % of the five-hour window per story on Pro (measured in the bake-off), so roughly 1–5 % of a window on a normal day. An `allowed_warning` status is logged once.
- **Never logged:** the token, the prompt, the posting text, OCR text and Claude's reply. The log has the media id, the post types, the duration, an exception's type and the CLI's exit code or status.
- **Install.** `install.sh` installs Claude Code's native binary, pinned to an exact version and checked by SHA-256, into `/opt/story-watch/claude/<version>` (root-owned), only when `.env` has `CLASSIFIER=claude`. It doesn't install or start `story-watch-llm` (that is for `hybrid` only).
- **Settings:** `CLAUDE_CODE_OAUTH_TOKEN` (required), `CLAUDE_MODEL` (default `opus`), `CLAUDE_TIMEOUT` (default 60), `TAGGER_OCR`, in `.env.example`.
- **Changing the prompt, schema, company lists or post-processing** means re-scoring first with `scripts/eval_tagger.py --tagger claude` (below) and bumping `CLAUDE_VERSION` in `story_watch/claude.py`; a test pins the scored prompt's hash.

### The hybrid tagger (`CLASSIFIER=hybrid`)

Chosen in #10 ([docs/research/tagger-comparison.md](docs/research/tagger-comparison.md)) and signed off by the user on 2026-10-10. Per new story, before it is posted (pings depend on the tags):

1. The still is downloaded (the same guards as the archive) and RapidOCR reads its text (`TAGGER_OCR`, about 1 s).
2. A local vision model, Qwen3-VL-4B Q4_K_M at 512 image tokens, gets the image, the link, the mentions and the posting's title, company, locations, the start of its text and its work-authorization sentences. It answers schema-constrained JSON. The prompt is `story_watch/prompts/tag_v1.txt`.
3. **The model decides the post type and the role. Rules decide the rest** (`story_watch/tag_rules.py`): company from the lists in `story_watch/companies.py` (the same as docs/tags.md), sponsorship from #9's posting rules plus the user's decisions (Tesla sponsors; "must be enrolled at a US school" and "US-based applicants only" mean `no_sponsor` unless the job is in or open to Canada), level from title words, role from title words when they settle it, and the guards from docs/tags.md (a link keeps `job_posting`; an unread posting's sponsorship is unsure, not `unknown`).
4. **Fallback, fail open.** If the model is down, times out (`LLM_TIMEOUT`, 90 s), answers junk, or the still couldn't be downloaded, the story gets the fallback tags: link and posting rules plus keyword rules on the OCR text (#10's `listing_rules`/`ocr_rules`, more extra pings). If even that raises, every tag is unsure. A story is never held past the timeout, and tags are stored at first classification, so a retried post keeps them.

The model runs in its own unit, `story-watch-llm.service`: llama.cpp's `llama-server` (release `b11514`, CPU build) as the user `storywatch-llm`, bound to `127.0.0.1:8090`, with `-t 4 -tb 4 -c 4096 -np 1 --cache-ram 0 --load-mode none --image-max-tokens 512`, `CPUQuota=400%`, `CPUWeight=20`, `Nice=10`, `MemoryMax=4608M`, `MemorySwapMax=0`, and `Restart=on-failure` every 60 s at most 5 times an hour. `install.sh` downloads the pinned binary and model files (2.9 GB, each checked by SHA-256, into `/opt/story-watch/llama.cpp` and `/opt/story-watch/models`) and enables the unit only when `.env` has `CLASSIFIER=hybrid`; with another classifier it stops and disables the unit and keeps the files.

- **Health and alerts.** The watcher checks the model's `/health` before every cycle. While it is down, stories get the fallback tags at once, without waiting on it. After 15 minutes down, one "Tagger model unreachable" alert goes to Discord, and "Tagger model recovered" follows when it is back. A brief outage (a deploy, a restart) is only logged.
- **Delay.** A new story is posted about 36 s later than with `rules` (57 s worst measured in CT 120), and never more than `LLM_TIMEOUT` plus the image download and OCR. Stories are tagged one at a time and each is posted as soon as it is tagged. After a timeout or a refused connection the model is skipped for the rest of the cycle, and a cycle spends at most 600 s waiting on the model; the stories after that get the fallback tags straight away.
- **Memory** (CT 120 has 6 GB): the model 3.8 GiB resident (capped at 4.5 GiB), RapidOCR in the watcher up to 0.75 GiB, the watcher and the bot about 0.1 GiB. See HUMANS.md.
- **Never logged:** the prompt, the posting text, the model's reply and the OCR text. The log has the media id, the post types, the latency, and an exception's type.
- **Settings:** `LLM_URL` (must be a local address), `LLM_MODEL`, `LLM_TIMEOUT`, `TAGGER_OCR`, in `.env.example`.
- **Changing the model, prompt or rules** means re-scoring first: `scripts/eval_tagger.py --tagger hybrid` (below).

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

This streams the repo, plus your local `.env` without its `TEST_*` lines, into the LXC and runs `deploy/install.sh`. The install script creates the `storywatch` and `storywatch-bot` users, `/opt/story-watch`, the venv, `.env` and the systemd unit. `.env` is your workstation's copy if you have one, otherwise `.env.example`. On the first run, it doesn't start the service. Instead it prints the remaining steps. To check that it worked, run `pct exec 120 -- id storywatch` on the host.

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
IG_USER_AGENT=<navigator.userAgent of the login browser>
```

`IG_USER_AGENT` is optional. For a session imported from a browser (method B), set it to that browser's exact User-Agent (in the browser's console, run `navigator.userAgent`), so Instagram sees one client instead of two: the GraphQL requests and the story-page fetch both send it. Leave it empty to use instaloader's default. When you log in again, update it to the browser you used, and keep it in sync after browser updates. It applies only to Instagram requests; image downloads and job-page fetches keep their own User-Agent.

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

To deploy a new version, run `PVE_HOST=root@<proxmox-host> CTID=120 ./deploy/push.sh` again, or `git pull && bash deploy/install.sh` inside the LXC. Both reinstall the code, upgrade instaloader and restart the service. `push.sh` also replaces `/opt/story-watch/.env` with your local `.env` (minus `TEST_*`) and keeps the previous one as `.env.bak`, so make config changes locally, not on the server. Before shipping, `push.sh` runs `story-watch --check-config` on that file and stops if the service would reject it (for example a malformed `DISCORD_WEBHOOK`; leftover `DISCORD_WEBHOOK_<n>`, `ROLE_*_<n>` or the removed role-ping variables only produce a warning). Once the service has stayed up for 5 seconds after the restart, `push.sh` posts "story-watch deployed" with the commit id to the Discord webhook (`-dirty` means uncommitted changes were deployed), followed by a changelog: only the commit subjects since the previous deploy, up to 15 (none if the previous deploy isn't known). `push.sh` ships the last 100 commits of `git log`, and `install.sh` records each deployed commit in `/opt/story-watch/deployed-commit` to know where the last deploy was. You can send it by hand with `story-watch --deploy-notify <commit>`. Neither touches the database or the session file, and `install.sh` run inside the LXC leaves `.env` alone.

## Story archive

Stories expire after 24 hours and their image URLs expire with them, so the service can keep its own copy for labeling and evaluating taggers. It is off unless `ARCHIVE_DIR` is set.

To enable it on the server, put this in your local `.env` and deploy:

```
ARCHIVE_DIR=/opt/story-watch/archive
ARCHIVE_MAX_MB=2048        # optional total cap, default 2048
```

`install.sh` creates `/opt/story-watch/archive` (owned by `storywatch`, group `storywatch-shared`, mode 3770; see [Users and secrets](#users-and-secrets)) and the systemd unit lets the service write there. For every new story, including ones that `NOTIFY_*` filters out, the watcher saves into `<ARCHIVE_DIR>/<target>/`:

- `<taken_at>_<media id>.jpg`: the full-resolution still image (for a video story, its poster frame; videos are never saved; images are capped at 25 MB). Older archives may still hold `.mp4` files from before; they are left alone.
- `<taken_at>_<media id>.json`: the sidecar with media id, target, `taken_at`, `is_video`, links, mentions, job title, company, the classifier name, its `tags` (`Tags.to_dict()`) and the old single `category` (kept for older readers), the saved file names, `download_error` (an error type, or null), the raw Instagram item `node` and `page_node` (with stickers), and `posting`: the job posting the watcher already extracted for classification (title, company, status, extractor `source`, locations, employment type, description capped at 20,000 characters (the extractor keeps up to 50,000) with a `description_truncated` flag, `is_event_page`), or null when the story had no readable link. It is never fetched again for the archive; `--backfill-archive` already runs the job lookup for its stories, so it stores the posting too. Sidecars from before this field have no `posting` key.

Archiving never delays or blocks a post. The copy is made in the same cycle that finds the story, after all accounts' posts have been sent (or skipped, or have failed and will be retried), so a slow download can't delay a post. Any error is logged by type only and skipped. Downloads use their own HTTP session with no Instagram cookies, https only, public addresses only, no redirects and the size caps above. The first run after you enable it only seeds the current stories and archives nothing; only stories that appear afterwards are saved. When the archive passes `ARCHIVE_MAX_MB`, the oldest items are deleted, so pull it regularly. The sidecars contain signed image URLs and the story media, so never commit them; a repo-local `archive/` is gitignored.

Stories that were already seen before the archive was enabled can be added for labeling with `story-watch --backfill-archive`. It fetches each target's live stories once and saves the ones with no sidecar yet (image, links, mentions, job info and tags, as above), then exits. It posts nothing to Discord, never touches the database and marks nothing seen, so the labeling bot just finds the new sidecars. It needs `ARCHIVE_DIR` and exits 1 on an Instagram error. Run it as the service user from `/opt/story-watch`, like `--test-notify` above (see HUMANS.md).

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
| `DB_PATH` | The watcher's `state.db` (same variable, default `/opt/story-watch/data/state.db`); `/pings` preferences, the `/nopost` list and the Fix tags message mapping are stored there |
| `DISCORD_WEBHOOK` | Optional for the bot (the watcher's variable): lets Fix tags edit the watcher's posts |

These are the only variables the bot reads (`BOT_ENV_KEYS` in `story_watch/bot/config.py`). On the server they are set in `.env` like everything else, and `install.sh` copies just these keys into the bot's own `/opt/story-watch/bot.env`; see [Users and secrets](#users-and-secrets).

**How labeling works.** Every 30 seconds the bot looks for archived stories it hasn't posted yet and posts them oldest first: the image (or, for an old archive item that has an `.mp4`, the video when it fits the server's upload limit), the links, mentions, job title and company, and the classifier's current guess. It remembers what it posted in `<ARCHIVE_DIR>/label-posts.jsonl`, so a restart doesn't repost. Press **Label** to open a form with one multi-select per tag dimension (post type, sponsorship, company, role, level; pick several for a story that covers several jobs). The post type's guess is pre-selected. The form saves nothing: it opens a private preview where you check the choices, optionally add a note, and press **Save**. Dimensions that don't apply to the chosen post type (`APPLICABLE` in `story_watch/tags.py`) are saved as not applicable; the others need at least one value. After saving, the post shows the labels and who saved them, and the button becomes **Relabel**; the last saved label for a story wins. Only users in `LABELER_USER_IDS` can label.

**Fix tags (#35).** The watcher's own posts carry a tag footer, and a webhook post can't have buttons, so corrections use a message context-menu command: right-click (or long-press) a story post, then **Apps -> Fix tags**. The watcher posts with `?wait=true` and stores `message id -> story` in the new `story_messages` table of `state.db` (created idempotently; old databases keep working; rows are pruned with the story after 48 h, so only recent posts can be fixed). The command opens the same tag form as **Label**, prefilled with the story's stored tags (a dimension the tagger had no idea about starts empty), then the same private preview, and **Save** confirms. Saving (1) appends a line to `labels.jsonl` with `"source": "correction"` and the labeler's id, so the fix joins the eval set (label-channel lines have no `source`; the last line per story still wins), (2) replaces the story's stored tags in `state.db`, and (3) edits the post's footer to the corrected tags plus "corrected", through the webhook's edit-message endpoint with no mentions allowed. It never pings anyone. Only `LABELER_USER_IDS` can use it; anyone else gets a private refusal. The bot needs `DISCORD_WEBHOOK` (the same variable, set in `.env` and copied into `bot.env`) to edit the post; if it is unset the fix is still saved and the confirmation says the post was not edited. If a fix sets FAANG+ or Quant and the story's company text matches nothing on that list (`story_watch/companies.py`), the private confirmation says so and the bot logs the company, so the list can be extended. The label channel is unchanged and stays on for now; it can be turned off later, once corrections replace hand-labeling.

**The no-post list (`/nopost`, #13).** One server-wide list of rules for stories the watcher records but never posts. It is not an env var: labelers (`LABELER_USER_IDS`) edit it in Discord, and it is stored in `state.db` (tables `nopost_rules`, `nopost_tags`, created idempotently). A rule is a conjunction of existing tag dimensions: for each dimension it names, a set of values, e.g. "Post type: Misc" or "Post type: Process info AND Level: Other". Values of one dimension are OR, dimensions are AND, and a story is dropped when any rule matches. Sponsorship and Job posting can't be used (job postings are never dropped, and only they have sponsorship). `/nopost show` prints the list for anyone in the server (read-only); `/nopost edit` (labelers only, everyone else gets a private refusal) has a multi-select of values for a new rule with **Add rule**, a select to remove a rule, and **Reset list** (press twice). All replies are private. The watcher reads the list from the DB every cycle; if it can't, it posts everything.

Safety rules, each tested in `tests/test_nopost.py`: a story is dropped only when the tagger is confident on every dimension the rule uses (its value set is inside the rule's values and isn't "every value"; not applicable never matches). A story with a job link, or whose post type could be Job posting, is always posted, and so are unsure tags (which include Job posting). Only a real model tagger's output can drop a story: `CLASSIFIER` must be `claude` or `hybrid`, and the tags must not be its fallback (`evidence` starting with `fallback:`). With `CLASSIFIER=rules`, or when Claude or the local model was down, the list drops nothing; the journal then says `no-post rule ... matched, but the tags are not a model tagger's` with the tag values. A dropped story is recorded as handled with `notified_at` NULL, its tags (values only, no text) are logged, it pings nobody, and it is still archived, so it still reaches the label channel and a wrong drop can be found and corrected. `NOTIFY_*` stays the deploy-time per-post-type switch; the list is applied after it. The eval harness reports how many labeled job postings a list would drop (must be 0), set with `--no-post` (rules separated by `;`, keys within a rule by `,`).

**Ping preferences (`/pings`, #16).** Each user sets two lists, stored per Discord user in `state.db` (tables `ping_users`, `ping_prefs`; the database now uses WAL, and the tables are created idempotently at start-up). `/pings edit` shows two multi-selects, "Ping me for" and "Never ping me for", over all 17 tag values; every pick saves at once, and picking a value on one list removes it from the other. A new user starts with "never ping me for: Sponsorship: No sponsor" offered (not saved until they change something). A ping list with no post type means job postings only. The reply warns when the ping list is empty (you won't be pinged) or when a whole dimension is blocked (you will never be pinged). `/pings show` prints the saved lists and `/pings clear` deletes them. The matching rules are in `docs/pings.md`; the bot only stores preferences, and pinging is #17. Commands are guild commands for the server of `LABEL_CHANNEL_ID`, synced on every start; the bot needs the `applications.commands` scope and no privileged intents.

**Label file.** `labels.jsonl` gets one JSON line per save (history is kept):

```json
{"media_id": "...", "target": "...", "post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["quant", "other"], "role": ["swe", "pm"], "level": ["internship"], "labeler": "<Discord user id>", "note": "", "labeled_at": "2026-01-01T12:00:00+00:00", "taxonomy": "tags-1a2b3c4d"}
```

Each dimension is a list of the stable value strings from `docs/tags.md` in enum order, or `null` when it doesn't apply to the post type. `taxonomy` is derived from the dimensions, values and `APPLICABLE`, so it changes whenever the taxonomy does. The file and `label-posts.jsonl` sit at the top level of the archive, not in the per-target folders, because the archive's size cap deletes files in those folders. `scripts/pull_archive.py` copies the whole archive directory, so the labels come along (and replace the local copy of those two files).

The unit restarts at most 5 times an hour, 2 minutes apart, and not at all after a config or token error (exit code 2), because Discord resets a bot token after 1000 gateway logins in 24 hours. Check the environment with `.venv/bin/story-watch-bot --check-config`; logs: `journalctl -u story-watch-bot`.

### Users and secrets

The watcher and the bot run as different users (#45), so a compromised bot can't read the Claude token or the Instagram session:

| | `story-watch` (watcher) | `story-watch-bot` (bot) |
|---|---|---|
| User | `storywatch` (home `/opt/story-watch`) | `storywatch-bot` (no home, no shell) |
| Env file | `/opt/story-watch/.env` (`root:storywatch`, 0640): everything, including `CLAUDE_CODE_OAUTH_TOKEN`, `IG_*`, `DISCORD_WEBHOOK` | `/opt/story-watch/bot.env` (`root:storywatch-bot`, 0640): only `BOT_ENV_KEYS`, i.e. `DISCORD_BOT_TOKEN`, the label settings, `ARCHIVE_DIR`, `DB_PATH` and `DISCORD_WEBHOOK` |
| Only it can read | `.env`, `.env.bak`, `.config/instaloader/` (the session), `data/claude-home/` | `bot.env` |

Both are in the group `storywatch-shared`, which holds exactly what they share: `data/` (mode 2770: `state.db` and its `-wal`/`-shm` files) and `archive/` (mode 3770; the per-target folders are 2750, so the bot reads stills and sidecars but can't add or remove files; `labels.jsonl` and `label-posts.jsonl` are the bot's, 0600). `install.sh` creates the users and the group, derives `bot.env` from `.env` on every deploy (an allowlist, so a new setting stays out of the bot until it is added to `BOT_ENV_KEYS`), and fixes owners and modes of an existing install; it changes nothing when run again. The bot's unit also makes `.env`, `.config`, `data/claude-home` and the Claude binary inaccessible, runs from `/` so the config's `.env` lookup never finds the watcher's file (an unreadable `.env` is skipped, not an error), and has no capabilities and a `@system-service` syscall filter.

SQLite across two users: the database is in WAL mode, so whichever service opens it first creates `state.db-wal` and `state.db-shm`, and the other must be able to write them. SQLite gives those files the database file's mode (0660), `data/` is setgid so they get the group `storywatch-shared`, and both units run with `UMask=0007`, so a `state.db` either service creates is 0660 as well. `data/` is not sticky, because SQLite deletes the `-wal` file on the last close, whoever created it. If you open the database by hand, do it as root or `storywatch`; root's `sqlite3` keeps the files' owner.

## Development

```bash
uv venv && uv pip install -e '.[dev]'    # or python -m venv .venv && pip install -e '.[dev]'
.venv/bin/pre-commit install             # once per clone: secret scan on every commit
.venv/bin/pytest
.venv/bin/story-watch --once             # one real cycle, using ./.env
.venv/bin/story-watch --backfill-archive # archive the live stories missing from ARCHIVE_DIR (see Story archive)
```

The pre-commit hook runs [gitleaks](https://github.com/gitleaks/gitleaks) with its default rules plus rules for Discord webhook URLs and Instagram `sessionid` cookies (`.gitleaks.toml`), and refuses `.env`, `session-*`, `*.har`, `*.db`, `story-dump*` and `story-cache/` even when they are force-added. The repo is public, so don't skip it with `--no-verify`. If it flags a test fixture, make the fixture look less real rather than adding an allowlist. `.venv/bin/pre-commit run --all-files` scans the whole tree.

### Testing embeds with `scripts/resend_story.py`

This script resends one of the target's current stories to a separate Discord webhook, so you can change the embed, job lookup or classifier and see the result without waiting for a new story. It runs on the workstation with the same burner session file and `.env` as `--once`, and never touches the service database.

Set these in `./.env` (the service ignores them):

```
TEST_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...   # a test channel
TEST_PING_USER_IDS=<user id>,<user id>   # optional: users to ping in the test server
```

```bash
.venv/bin/python scripts/resend_story.py --list            # numbered stories, 1 = most recent
.venv/bin/python scripts/resend_story.py -n 3              # send the 3rd most recent
.venv/bin/python scripts/resend_story.py -n 1-5,8          # several at once
.venv/bin/python scripts/resend_story.py -n 3 --dry-run    # print the Discord payload, send nothing
.venv/bin/python scripts/resend_story.py -n 3 --no-ping    # don't ping TEST_PING_USER_IDS
.venv/bin/python scripts/resend_story.py --refresh -n 1    # re-fetch from Instagram first
```

- **Instagram is only contacted on the first run or with `--refresh`.** The raw stories are cached in `story-cache/<target>.json` (gitignored). Every run rebuilds the embed from that cache with the current code, so edits to `notify.py`, `jobs.py` or `classify.py` show up on the next run. Job pages are fetched live each time.
- **Thumbnail URLs in the cache expire after about a day.** Use `--refresh` when images stop loading.
- **`CLASSIFIER=claude` asks Claude for real, even with `--dry-run`** (one `claude -p` call per story on the owner's plan, with `CLAUDE_CODE_OAUTH_TOKEN` from `./.env` and the `claude` on your PATH). Claude's private HOME is `~/.cache/story-watch/claude-home`. A story Claude couldn't tag is marked "(fallback tags)" in the output. Run `CLASSIFIER=claude .venv/bin/python scripts/resend_story.py -n 1-3 --dry-run` to try it without changing `.env`.
- **Every selected story is sent, even if `NOTIFY_*` would filter it.** The output says when the service would skip it.
- **Pings come only from `TEST_PING_USER_IDS`.** Every story pings exactly those users in the test server. The script never opens the service database, so it can't read or ping real users' preferences.
- **For each story, it prints the post type, company, job title, links and the number of pinged users to the terminal.** The webhook URL is never printed.

### Scoring a tagger with `scripts/eval_tagger.py`

Runs a tagger over every labeled story and prints a Markdown report (`--json` for JSON, `--out FILE` to also save it). It reads `labels.jsonl` and the archive sidecars from the archive directory you pass (default `~/story-watch-data/archive`, i.e. what `scripts/pull_archive.py` pulled) and makes no network requests.

```bash
.venv/bin/python scripts/eval_tagger.py                              # the current classifier ("rules")
.venv/bin/python scripts/eval_tagger.py --tagger unsure              # baseline: "no idea" for everything
.venv/bin/python scripts/eval_tagger.py --tagger my_module:make_tagger   # a research prototype
.venv/bin/python scripts/eval_tagger.py --tagger hybrid --images ~/story-watch-data/eval-work/issue-10/images
.venv/bin/python scripts/eval_tagger.py --tagger claude --replies ~/story-watch-data/eval-work/claude-bakeoff/raw/opus-v1 \
    --images ~/story-watch-data/eval-work/issue-10/images
```

`--tagger hybrid` runs the production `HybridClassifier` with no network: the posting comes from the sidecar's `posting` when it has one, else from the listing saved per story (`--listings`, default `~/story-watch-data/listings`), the still from `--images` or the archive, the OCR text from RapidOCR (or `--ocr-dir`, saved text), and the model replies from the llama-server at `LLM_URL` (run it on the workstation CPU with the unit's flags, never the GPU) or from saved replies (`--replies DIR`). `--tagger claude` does the same for the production `ClaudeClassifier`: with `--replies` it replays the bake-off's saved `claude -p` records through the classifier's own parser, validation and Canada safety net, with no Claude call; without it, it calls `claude -p` live with `CLAUDE_CODE_OAUTH_TOKEN`, which uses the owner's plan.

- **A tagger** has `name`, `version` and `tag(story) -> Tags` (`tag` may return `TaggerOutput(tags, cost_usd)` to report a cost). `--tagger` takes a classifier name from `CLASSIFIERS` (wrapped by an adapter that maps the category to a post type and leaves the other dimensions unsure), `unsure`, or `package.module:factory`. A tagger that raises is scored as `Tags.unsure()`, like the watcher does.
- **Report.** Missed pings (the headline: the true tags would ping a user and the predicted tags would not) and extra pings per user config, per-dimension accuracy ("exact", and "covers" when no labeled value was ruled out), confusion matrices, job postings the "no post" list would drop (must be 0; the real #13 function, set with `--no-post "post_type:misc;post_type:process_info,level:other"`; like production, only `claude`/`hybrid` output that isn't their fallback can drop a story), latency and cost, and labeled stories per value with rare values flagged (`--min-count`, default 10).
- **Reading the accuracy numbers.** Stories labeled N/A for a dimension (e.g. sponsorship on an event) are left out of that dimension's accuracy. A tagger that says N/A for a dimension the label applies to ("Said N/A") counts as a miss. The reverse (the tagger gave values where the label is N/A) is not part of accuracy; it is shown as its own column. In a confusion matrix the `multi` row is a story labeled with several values, the `multi` column is a tagger answer with several values (including "no idea"), and `n/a` is the tagger saying the dimension does not apply.
- **User configs** are in `eval/ping_configs.toml` (`--ping-configs` for another file) and are matched with `should_ping` from `story_watch/pings.py`.
- **Cache.** Outputs are cached in `~/story-watch-data/eval-cache/<tagger>/<version>/<media id>.json` (`--cache-dir`, outside the repo). Change a tagger's `version` when its behavior changes; the `classify.py` adapter includes a hash of that file, so editing it invalidates its cache. `--refresh` re-runs and overwrites, `--no-cache` skips the cache. Cached latency is the one measured when the entry was written.
- **Which labels count.** Labels written under another taxonomy version are left out (`--allow-stale-taxonomy` includes them), as are labels that don't parse. The report says how many.

## Layout

| Path | Purpose |
|---|---|
| `story_watch/config.py` | Loads and validates env / `.env` |
| `story_watch/instagram.py` | instaloader session, cached user ID lookup, `fetch_story_items()`, story-page links and mentions (`with_extras()`) |
| `story_watch/classify.py` | The `Classifier` interface, the registry, and the placeholder rule-based classifier |
| `story_watch/claude.py`, `prompts/tag_claude_v1.*` | The Claude tagger: the sandboxed `claude -p` call, stream parsing, strict schema validation, the Canada safety net, the fallback |
| `story_watch/hybrid.py`, `llm.py`, `ocr.py`, `tag_rules.py`, `posting_rules.py`, `companies.py`, `prompts/` | The hybrid tagger: classifier and fallbacks, the llama-server client and request, RapidOCR, the rules, #9's posting rules, the company lists |
| `docs/tags.md` | Story tag taxonomy, labeling guide and classifier output contract (`story_watch/tags.py`) |
| `story_watch/jobs.py` | Job-posting lookup for link stickers (title, company, posting text from ATS APIs / JSON-LD / page text), with SSRF guards and DNS pinning |
| `scripts/dump_story.py` | Diagnostic: dump raw GraphQL and story-page JSON for a target |
| `scripts/resend_story.py` | Re-send the Nth most recent story to `TEST_DISCORD_WEBHOOK`, from a local cache, to iterate on embeds |
| `story_watch/archive.py` | Saves each new story's media and a JSON sidecar when `ARCHIVE_DIR` is set (size-capped, no cookies) |
| `scripts/pull_archive.py` | Pull the archive from the CT to `~/story-watch-data/archive/` over ssh + `pct exec` |
| `scripts/seed_test_archive.py` | Write fake stories (sidecar + placeholder PNG) to `~/story-watch-data/test-archive/` for trying the bot locally |
| `story_watch/store.py` | SQLite tables `seen` and `targets`, plus pruning after 48 h |
| `story_watch/notify.py` | Discord embeds, with retries on 429 (`retry_after`) and 5xx |
| `story_watch/main.py` | Loop, backoff, alerts, heartbeat, SIGTERM handling, CLI |
| `story_watch/bot/` | Discord bot (`story-watch-bot`): `labels.py` (label format and validation), `queue.py` (archive scan, posted log), `render.py`, `config.py`, `discord_app.py` (discord.py glue) |
| `story_watch/evaluation/` | Scoring harness for taggers (`data`, `taggers`, `cache`, `runner`, `metrics`, `report`, `cli`); run it with `scripts/eval_tagger.py`; user configs in `eval/ping_configs.toml` |
| `deploy/` | systemd units (`story-watch`, `story-watch-bot`, `story-watch-llm`), idempotent `install.sh`, `push.sh` to deploy from a workstation |

On the server, `/opt/story-watch` contains `app/` (code, owned by root), `.venv/`, `.env` (mode 0640), `bot.env` (the bot's subset of `.env`), `data/state.db`, `data/claude-home/` and `claude/<version>/` (with `CLASSIFIER=claude`), `archive/` (see below) and `.config/instaloader/session-<burner>`.
