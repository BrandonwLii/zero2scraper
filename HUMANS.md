# story-watch, the short version

Posts new Instagram stories to Discord, tagged by Claude (`CLASSIFIER=claude`, live since
2026-10-11). Runs in CT 120 on Proxmox: `story-watch` (the watcher) and `story-watch-bot`
(labels, `/pings`, `/nopost`, Fix tags).
Full details in README.md.

## Daily stuff

Logs:

    ssh root@pve pct exec 120 -- journalctl -u story-watch -n 100 --no-pager
    ssh root@pve pct exec 120 -- journalctl -u story-watch-bot -n 50 --no-pager

If `ssh root@pve` just hangs, Tailscale SSH wants you to re-authenticate (it prints a
`login.tailscale.com` URL), or Tailscale on Windows is logged out. Quick check that doesn't hang:
`timeout 15 ssh -o BatchMode=yes root@pve true`.

Deploy a new version:

    PVE_HOST=root@pve CTID=120 ./deploy/push.sh

When it's done, Discord gets "story-watch deployed" with the commit id and
the commits since the last deploy. Don't label or Fix tags during a deploy: both
services restart. No
message means the service didn't stay up; check the logs.

Config: edit `.env` on the workstation, then deploy. `push.sh` copies it to
`/opt/story-watch/.env` in the CT (without the `TEST_*` lines) and restarts the
service. The previous server copy is kept as `.env.bak`. Edits made directly on
the server are overwritten by the next deploy.

## Got a "re-login" alert

1. Log in to the burner on instagram.com in Firefox, clear any challenge, close Firefox.
2. On the workstation:

       .venv/bin/instaloader --load-cookies firefox --cookiefile /mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite

3. Push the session:

       scp ~/.config/instaloader/session-<burner> root@pve:/root/
       ssh root@pve 'pct push 120 /root/session-<burner> /opt/story-watch/.config/instaloader/session-<burner> --user storywatch --group storywatch --perms 0600 && rm /root/session-<burner>'

No restart needed. You'll get "recovered" in Discord.

## Common .env changes

| Want to | Set |
|---|---|
| Watch someone | `TARGETS=user1,user2:<userid>` (userid avoids 429s) |
| Mute a post type | `NOTIFY_MISC=false` (or `_EVENT`, `_JOB_POSTING`, `_PROCESS_INFO`; `_INTERVIEW_INFO` is the old name of `_PROCESS_INFO`) |
| Stop posting a kind of story (e.g. Misc) | Nothing in `.env`: a labeler uses `/nopost edit` in Discord (see below). `NOTIFY_*` stays as the deploy-time switch for a whole post type. |
| Change who gets pinged | Nothing in `.env`: each person uses `/pings` in the bot. The old `PING_ROLES` and `ROLE_*` settings are gone; delete them. |
| Daily "alive" ping | `HEARTBEAT_HOUR=9` |
| Keep a copy of every story (still images only, no video) | `ARCHIVE_DIR=/opt/story-watch/archive` (`ARCHIVE_MAX_MB=2048` caps it) |
| Tag stories with Claude (the chosen tagger) | `CLASSIFIER=claude` and `CLAUDE_CODE_OAUTH_TOKEN` (see below) |
| Tag stories with the local model | `CLASSIFIER=hybrid` (see below) |
| Mark new stories seen on Instagram (the burner shows in the viewer list) | `MARK_SEEN=true`, only after the HAR check below (default `false`) |

## Before turning on `MARK_SEEN`

The request copies the web app's `PolarisStoriesV3SeenMutation`, whose `doc_id` changes with
Instagram's web releases. Check it once against your own browser, logged in as the burner:

1. Firefox, DevTools > Network, filter `graphql`. Open a story of someone the burner follows and
   let it play.
2. Find the POST to `/api/graphql` whose `fb_api_req_friendly_name` is
   `PolarisStoriesV3SeenMutation`. Compare with `story_watch/instagram.py`: the `doc_id`
   (`SEEN_DOC_ID`), the five `variables` (`reelId`, `reelMediaId`, `reelMediaOwnerId`,
   `reelMediaTakenAt`, `viewSeenAt`) and their types, and the headers (`X-CSRFToken`,
   `X-IG-App-ID`, `X-FB-LSD`, `Referer`). The reply should be
   `{"data":{"xdt_mark_story_reel_seen":{...}}}`.
3. If you saved a `.har`, it holds the session cookies: keep it out of git and delete it.

If they differ, update the code and `tests/test_mark_seen.py`, deploy, then set `MARK_SEEN=true`.
Afterwards `journalctl -u story-watch | grep "seen on Instagram"` shows each marked batch; a
failure logs `marking N item(s) seen failed` and the story is still posted.

## The Claude tagger (`CLASSIFIER=claude`)

Claude Opus tags each new story through Claude Code, headless, on **your own Claude plan**. Each
story is one call: about 0.15–0.3 % of a five-hour window on Pro, so a normal day (about 25
stories) uses a few percent. It shares the plan's limits with your own Claude use.

1. **Get a token** on your workstation: `claude setup-token`, then paste it into `./.env` as
   `CLAUDE_CODE_OAUTH_TOKEN=...`. Treat it like the webhook: never share it, paste it or commit it
   (`.env` is gitignored). You can revoke it from your claude.ai account settings.
2. **Try it** on cached stories without posting anything (this does ask Claude, once per story):

       CLASSIFIER=claude .venv/bin/python scripts/resend_story.py -n 1-3 --dry-run

3. **Switch:** set `CLASSIFIER=claude` in `./.env` and deploy. `install.sh` installs a pinned
   Claude Code (about 250 MB, checked by SHA-256) into `/opt/story-watch/claude/` and stops the
   local model unit if it was running. `push.sh` refuses to deploy `CLASSIFIER=claude` without a token.
4. **Check** the first stories (production did this on 2026-10-11; the first live story took 7.5 s): `journalctl -u story-watch -n 100 --no-pager | grep tagger` in the CT
   should show "claude tagged <id> in 5.2s"; "used the fallback rules (...)" means Claude didn't answer.

Optional settings: `CLAUDE_TIMEOUT=60` (seconds per story before the fallback tags),
`CLAUDE_MODEL=opus` (leave it: the scores are for Opus), `TAGGER_OCR=true` (OCR for the fallback).

If Claude can't answer (usage limit, outage, timeout, an odd reply), stories still go out with
simpler tags (more extra pings). After 15 minutes of that you get one "Claude tagger unavailable"
alert, then "Tagger model recovered" once a story is tagged by Claude again. With `HEARTBEAT_HOUR`
set, the daily heartbeat also says how many stories got the fallback tags.

**A new token** (it expires after a year, or after you revoke it): run `claude setup-token` again,
replace the line in `./.env`, deploy. **A newer Claude Code:** see the comment above
`CLAUDE_CLI_VERSION` in `deploy/install.sh`.

**Turn it off:** set `CLASSIFIER=rules` and deploy. The binary stays in `/opt/story-watch/claude/`.

## The model tagger (`CLASSIFIER=hybrid`)

Not used in production since Claude took over (2026-10-11); the model files were deleted from
the CT. Before switching back, note that since #45 `/opt/story-watch` is closed to other users,
so `story-watch-llm` (its own user) can't reach `llama.cpp/` and `models/` there: move them out
of `/opt/story-watch` first (a code change in `install.sh` and the unit).

Set `CLASSIFIER=hybrid` in `.env` and deploy. `install.sh` then downloads llama.cpp and the
Qwen3-VL-4B model once (2.9 GB, checked by SHA-256) and starts `story-watch-llm`, a local-only
model server. Each new story is posted about 40 s later than before, with real tags. Set it back to
`rules` and deploy to turn it off (the unit is stopped; the files stay in `/opt/story-watch/models`).

Optional settings: `LLM_TIMEOUT=90` (seconds per story before the fallback tags), `TAGGER_OCR=true`
(RapidOCR on each image), `LLM_URL=http://127.0.0.1:8090` and `LLM_MODEL` (leave them).

If the model is down, stories still go out with simpler tags (more extra pings). After 15 minutes
down you get one "Tagger model unreachable" alert, then "Tagger model recovered". Look at it with:

    ssh root@pve pct exec 120 -- systemctl status story-watch-llm
    ssh root@pve pct exec 120 -- journalctl -u story-watch-llm -n 50 --no-pager

It stops restarting after 5 failures in an hour; fix the cause, then
`systemctl reset-failed story-watch-llm && systemctl start story-watch-llm` in the CT.

**Memory.** CT 120 has 6 GB. The model uses 3.8 GiB (hard cap 4.5 GiB), OCR in the watcher up to
0.75 GiB, the watcher and the bot about 0.1 GiB, so about 0.5 GiB is left at the worst moment. Don't
run anything else heavy in the CT (no second model, no benchmarks). Giving the CT 8 GB would remove
the squeeze, if the host can spare it.

To try it on real stories before deploying, run the model on the workstation (CPU only) and use
`resend_story.py` with `CLASSIFIER=hybrid` in your local `.env`:

    llama-server -m <Qwen3VL-4B-Instruct-Q4_K_M.gguf> --mmproj <mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf> \
        --host 127.0.0.1 --port 8090 -t 4 -tb 4 -c 4096 -np 1 --cache-ram 0 --load-mode none --image-max-tokens 512
    .venv/bin/python scripts/resend_story.py -n 1 --dry-run

## Pull the story archive

Needs `ARCHIVE_DIR` set and deployed. Stories only land in it from then on (the first run after enabling just seeds).

    PVE_HOST=root@pve CTID=120 .venv/bin/python scripts/pull_archive.py

Files go to `~/story-watch-data/archive/` (override with `ARCHIVE_PULL_DIR`). Each story's JSON sidecar also holds the job posting the watcher read (title,
company, locations, description up to 20,000 characters), so re-scoring a tagger doesn't need the
job pages again. Re-run any time; it
only adds. Never commit it: the JSON has signed image URLs. The server deletes the oldest items past
`ARCHIVE_MAX_MB`, so pull now and then.

## Backfill the label archive

Adds the stories that are live right now but not in the archive yet (the archive only started at
the last deploy) so the bot can offer them for labeling. Posts nothing, writes no database, marks
nothing seen. Needs `ARCHIVE_DIR` in the CT's `.env`. From the Proxmox host (or drop the
`pct exec 120 --` part from a root shell in the CT):

    pct exec 120 -- sh -c 'cd /opt/story-watch && runuser -u storywatch -- env HOME=/opt/story-watch .venv/bin/story-watch --backfill-archive'

It reads `/opt/story-watch/.env` itself (from the working directory), so don't source it. Run it
as `storywatch`, never as root, so the new files are readable by the service and the bot. Safe to
re-run; it only adds. It prints one line per target (`N live, M already archived, K archived now`).

## Labeling bot setup

One-time, per Discord server. You label stories in a private channel; the answers become the ground truth for scoring taggers.

1. Discord Developer Portal (https://discord.com/developers/applications) -> **New Application**, name it.
2. **Bot** tab -> leave all three **Privileged Gateway Intents** off. Click **Reset Token** and copy it once. It goes only in `.env` as `DISCORD_BOT_TOKEN` (`install.sh` copies it, with the other bot settings, into the bot's own `bot.env`); never paste it in chat, issues or commits. If it leaks, reset it here.
3. **OAuth2 -> URL Generator**: tick the scopes `bot` and `applications.commands` and nothing else (the second is for `/pings`; if the bot is already in your server, open the new URL once to add it). Under *Bot Permissions* tick only View Channel, Send Messages, Attach Files, Read Message History and Embed Links. Open the generated URL and add the bot to your server.
4. Make a private channel (e.g. `#story-labels`), visible only to you, the people who may label and the bot. If the invite gave the bot server-wide permissions, remove them and grant those five permissions on this channel only.
5. Discord **Settings -> Advanced -> Developer Mode**. Right-click the channel -> **Copy Channel ID**. Right-click each person who may label -> **Copy User ID**.
6. Put in `.env` (and `ARCHIVE_DIR` must already be set):

       DISCORD_BOT_TOKEN=<token>
       LABEL_CHANNEL_ID=<channel id>
       LABELER_USER_IDS=<your id>,<friend's id>

7. Deploy as usual (`PVE_HOST=root@pve CTID=120 ./deploy/push.sh`). `install.sh` starts `story-watch-bot` only when the token is set. First start posts the newest 10 archived stories; set `LABEL_BACKLOG_MAX` or `LABEL_SINCE` to change that.
8. Check: `ssh root@pve pct exec 120 -- journalctl -u story-watch-bot -n 50 --no-pager` should say "connected to Discord".

Using it: press **Label** under a story, pick values, **Submit**, check the private preview, optionally **Add note**, then **Save**. Nothing is saved before Save. **Relabel** changes a label later. Pull the labels with the archive (`labels.jsonl` is in it).

**Fix tags (on the watcher's own posts).** Right-click a story post (long-press on mobile) -> **Apps -> Fix tags**, pick the right values, **Submit**, check the private preview and press **Save**. That records a correction label (`source: "correction"` in `labels.jsonl`, pulled with the archive), updates the stored tags and changes the post's footer to the corrected tags with "corrected". It pings nobody. Only `LABELER_USER_IDS` can do it, and only for posts from the last 48 hours that the watcher made after this version was deployed (older posts aren't in the database). No new setup: the bot gets `DISCORD_WEBHOOK` from `.env` (through `bot.env`) to edit the post, and the command is registered for the server of `LABEL_CHANNEL_ID` on every start (look for "registered /pings" in the journal; if "Fix tags" is missing, re-check `applications.commands`). If you set FAANG+ or Quant for a company the lists don't know, the confirmation tells you, and the journal logs it; add the company to `story_watch/companies.py` and `docs/tags.md` together.

**Pings (`/pings`).** The same bot has `/pings edit`, `/pings show` and `/pings clear`, usable by everyone in the server; replies are private. No extra setup or env var: the commands are registered for the server of `LABEL_CHANNEL_ID` on every start, and preferences go in the watcher's `state.db` (`DB_PATH`). Each new story pings the people whose preferences match its tags. If `/pings` doesn't appear, check the journal for "registered /pings" and that the bot was invited with `applications.commands`.

**The no-post list (`/nopost`).** Labelers (`LABELER_USER_IDS`) decide which stories are never posted. `/nopost edit`: pick the values for one rule in the select, check the "New rule" line, press **Add rule**. Different kinds are "and" (Post type: Process info + Level: Other means process-info stories at other levels), several values of one kind are "or". Remove a rule with the second select; **Reset list** empties it (press twice). `/nopost show` lets anyone read the list. No env var and no setup: it is stored in `state.db` and the watcher picks changes up on its next cycle. Safe by design: stories with a job link, stories that might be a job posting, and stories the tagger is unsure about are always posted. The list only acts when `CLASSIFIER=claude` or `hybrid` actually tagged the story: with `CLASSIFIER=rules`, or while Claude or the local model is down (fallback tags), nothing is dropped and the journal says `matched, but the tags are not a model tagger's`. A dropped story is still archived and still shows up in the label channel, so if the list is too eager you can see it there; the journal logs `dropped by the no-post list` with the tag values. If `/nopost` doesn't appear, look for "registered /pings and /nopost" in the bot journal.

**A separate test bot for development.** Make a second application and bot the same way, invite it to a throwaway server with its own private channel, and use its token and ids in your local `.env`. Run `.venv/bin/story-watch-bot` on the workstation against a scratch `ARCHIVE_DIR` (a copy of a pulled archive, or fake stories from `.venv/bin/python scripts/seed_test_archive.py`, which writes to `~/story-watch-data/test-archive`). Never run two copies with the same token, and don't point a dev copy at the production archive.

If the bot exits with "config error" or "rejected the bot token" it will not restart by itself; fix `.env`, redeploy, or `systemctl reset-failed story-watch-bot && systemctl start story-watch-bot` in the CT.

**The bot's own user (#45).** The bot runs as `storywatch-bot`, not `storywatch`, and gets only `/opt/story-watch/bot.env`, which `install.sh` writes on every deploy from `.env` with just the bot's keys. It can't read `.env`, the Instagram session or Claude's files. Keep editing `.env` on the workstation as before; never edit `bot.env` (the next deploy overwrites it). If the bot needs a new setting, add its key to `BOT_ENV_KEYS` in `story_watch/bot/config.py`, or it won't reach the bot. The two users share only `data/` (the database) and the archive, through the group `storywatch-shared`. To look at the database by hand in the CT, use root or `runuser -u storywatch --`, never another user. If `journalctl -u story-watch-bot` shows `status=226/NAMESPACE` or `218/CAPABILITIES` after a deploy, the CT doesn't support one of the bot unit's hardening lines; say which, and drop that line (`SystemCallFilter` or `CapabilityBoundingSet` are the likely ones).

## Dev

    .venv/bin/pre-commit install                        # once: blocks commits with secrets
    .venv/bin/pytest
    .venv/bin/python scripts/resend_story.py --list     # see stories
    .venv/bin/python scripts/resend_story.py -n 1       # resend to TEST_DISCORD_WEBHOOK
                                                        # (pings TEST_PING_USER_IDS if set; --no-ping skips)

## Don't

- Use your real Instagram account.
- Share the webhook URL or session file.
- Restart while rate-limited.
