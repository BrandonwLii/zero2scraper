# story-watch

This repo is developed in WSL and deployed to an unprivileged Debian 12 LXC (CT 120) on a Proxmox host that is reachable over Tailscale. Claude Code runs only on the workstation; it is not installed on the server.

- Tests: `.venv/bin/pytest`. Tests must never touch the network; mock instaloader and HTTP.
- Deploy: `PVE_HOST=root@<proxmox> CTID=120 ./deploy/push.sh`. It runs `deploy/install.sh` inside the CT, and that script must stay idempotent.
- Remote logs: `ssh root@<proxmox> pct exec 120 -- journalctl -u story-watch -n 100 --no-pager`
- Target is Python 3.11 (Debian 12). Don't use newer syntax or stdlib features.
- Never log or put the Discord webhook URL or the session contents in exception messages. requests exceptions include the URL, so log only their type.
- An item is marked seen only after `DISCORD_WEBHOOK` returns 2xx, or when every post type it could have is filtered out by `NOTIFY_*` (recorded with `notified_at` NULL).
- Instagram errors back off exponentially (cap 3600 s). Session or checkpoint errors send one alert and do not crash the service.

## Instagram

- `web_profile_info` (`Profile.from_username`) returns 429 for this account. Targets carry their userid in `TARGETS` (`name:userid`), so the lookup is skipped.
- Don't call `Story.get_items()`. It also requests the iPhone `reels_media` endpoint, which returns an empty reel for this session and raises `KeyError`. Read `story._node["items"]` instead.
- The GraphQL feed has no stickers. Link stickers (`story_link_stickers`) and @mentions (`story_bloks_stickers`) come only from the JSON embedded in the `/stories/<user>/` HTML page. Fetch that page only when a cycle has new items. A failed page fetch drops links and mentions, but must never block a notification.
- Never mark stories seen on Instagram. Plain HTML fetches run no JavaScript, so they don't. Don't add requests to `xdt_mark_story_reel_seen` or similar endpoints.

## Job pages and classification

- Job-page fetches (`story_watch/jobs.py`) go to arbitrary sites from inside the home network. They must never carry Instagram cookies, must be https-only, must check for a public address on every redirect hop, and must stay size-capped. Keep these guards.
- Classifiers implement `classify(item) -> Tags` (`story_watch/tags.py`, contract in `docs/tags.md`) and are registered in `CLASSIFIERS` in `story_watch/classify.py`. An LLM classifier is planned. Each dimension is the set of values the classifier can't rule out; an empty set is invalid and doubt is expressed by widening the set. `rules` is a placeholder: any link means post type `{job_posting}`, everything else `{misc}`, and all other dimensions are left unsure. It must not guess `process_info`, because keyword rules only produced false positives. If a classifier raises, the watcher uses `Tags.unsure()` (fail open: the story is still posted and pings every user who could match). Tags are stored per media id at first classification and reused on retries. `NOTIFY_*` acts on the post type only; `*_INTERVIEW_INFO` are deprecated aliases of `*_PROCESS_INFO`.
- The project supports exactly one Discord server (decided 2026-10-08). Stories, deploy messages, alerts, heartbeat and `--test-notify` all go to `DISCORD_WEBHOOK`. Leftover `DISCORD_WEBHOOK_<n>` / `ROLE_<TYPE>_<n>`, and the removed role pings (`PING_ROLES`, `ROLE_<TYPE>`), only log a warning naming the variable. An old `deliveries` table may remain in existing databases; the code ignores it.
- Discord pings are user mentions only: `<@id>` at the end of `content`, with `allowed_mentions` set to `{"parse": [], "users": [ids]}`. Never let Discord parse everyone or roles. The users are those whose `/pings` preferences (`Store.all_ping_prefs`, `should_ping`) match the story's stored tags. Fail open: if the preferences can't be read, post without pings and log the exception type only. Mentions that don't fit (100 ids or 2000 characters) go in best-effort follow-up messages, and the item counts as delivered once the main post returns 2xx. Alerts, the heartbeat, `--test-notify`, deploy messages and `NOTIFY_*`-filtered stories ping nobody. Only `scripts/resend_story.py` reads `TEST_PING_USER_IDS` and `TEST_DISCORD_WEBHOOK`, and it never reads real users' preferences. The service must never use them.

## Discord bot

- `story-watch-bot` (`story_watch/bot/`) is a gateway bot for one Discord server only. Labeling is its first feature; `/pings` (#16) goes in the same bot. Posting stories stays on webhooks.
- `DISCORD_BOT_TOKEN` follows the webhook rule: never log it, echo it or put it in exception messages; log exception types only. The unit must not restart fast or crash-loop (1000 IDENTIFYs per 24 h resets the token), and config or token errors exit 2 without a restart.
- Anything the bot writes goes at the top level of `ARCHIVE_DIR`, never in the per-target folders (`Archive.prune()` evicts `*/*`). Keep pure logic (`labels.py`, `queue.py`, `render.py`, `config.py`) free of discord imports. A person must confirm every label with Save.

## Workflow

- Iterate on embed, job and classifier output with `scripts/resend_story.py`. It caches stories in `story-cache/` and makes no Instagram requests unless given `--refresh`. Use `scripts/dump_story.py` to inspect raw Instagram JSON.
- When adding an env var, update `.env.example`, the README and `HUMANS.md` in the same change.
- `*.har` captures contain session cookies. Keep them gitignored, and delete them after use.
- A pre-commit hook (gitleaks + `.gitleaks.toml`, plus a filename block) scans every commit. Never bypass it with `--no-verify`; fake webhook URLs in tests must stay short (e.g. `/api/webhooks/1/secret`) so they don't match.
