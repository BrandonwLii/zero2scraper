# story-watch

This repo is developed in WSL and deployed to an unprivileged Debian 12 LXC (CT 120) on a Proxmox host that is reachable over Tailscale. Claude Code runs only on the workstation; it is not installed on the server.

- Tests: `.venv/bin/pytest`. Tests must never touch the network; mock instaloader and HTTP.
- Deploy: `PVE_HOST=root@<proxmox> CTID=120 ./deploy/push.sh`. It runs `deploy/install.sh` inside the CT, and that script must stay idempotent.
- Remote logs: `ssh root@<proxmox> pct exec 120 -- journalctl -u story-watch -n 100 --no-pager`
- Target is Python 3.11 (Debian 12). Don't use newer syntax or stdlib features.
- Never log or put the Discord webhook URL or the session contents in exception messages. requests exceptions include the URL, so log only their type.
- An item is marked seen only after Discord returns 2xx, or when its category is filtered out by `NOTIFY_*` (recorded with `notified_at` NULL).
- Instagram errors back off exponentially (cap 3600 s). Session or checkpoint errors send one alert and do not crash the service.

## Instagram

- `web_profile_info` (`Profile.from_username`) returns 429 for this account. Targets carry their userid in `TARGETS` (`name:userid`), so the lookup is skipped.
- Don't call `Story.get_items()`. It also requests the iPhone `reels_media` endpoint, which returns an empty reel for this session and raises `KeyError`. Read `story._node["items"]` instead.
- The GraphQL feed has no stickers. Link stickers (`story_link_stickers`) and @mentions (`story_bloks_stickers`) come only from the JSON embedded in the `/stories/<user>/` HTML page. Fetch that page only when a cycle has new items. A failed page fetch drops links and mentions, but must never block a notification.
- Never mark stories seen on Instagram. Plain HTML fetches run no JavaScript, so they don't. Don't add requests to `xdt_mark_story_reel_seen` or similar endpoints.

## Job pages and classification

- Job-page fetches (`story_watch/jobs.py`) go to arbitrary sites from inside the home network. They must never carry Instagram cookies, must be https-only, must check for a public address on every redirect hop, and must stay size-capped. Keep these guards.
- Classifiers implement `classify(item) -> Category` and are registered in `CLASSIFIERS` in `story_watch/classify.py`. An LLM classifier is planned. `rules` is a placeholder: any link means `job_posting`, everything else `misc`. It must not guess `interview_info`, because keyword rules only produced false positives. If a classifier raises, the watcher treats the story as `misc`.
- Discord pings go through `allowed_mentions.roles` only, with role mentions at the end of `content`. Only `scripts/resend_story.py` reads `TEST_ROLE_*` and `TEST_DISCORD_WEBHOOK`. The service must never use them.

## Workflow

- Iterate on embed, job and classifier output with `scripts/resend_story.py`. It caches stories in `story-cache/` and makes no Instagram requests unless given `--refresh`. Use `scripts/dump_story.py` to inspect raw Instagram JSON.
- When adding an env var, update `.env.example`, the README and `HUMANS.md` in the same change.
- `*.har` captures contain session cookies. Keep them gitignored, and delete them after use.
