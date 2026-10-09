# story-watch, the short version

Posts new Instagram stories to Discord. Runs in CT 120 on Proxmox.
Full details in README.md.

## Daily stuff

Logs:

    ssh root@pve pct exec 120 -- journalctl -u story-watch -n 100 --no-pager

Deploy a new version:

    PVE_HOST=root@pve CTID=120 ./deploy/push.sh

When it's done, Discord gets "story-watch deployed" with the commit id and
the commits since the last deploy. No
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
| Mute a post type | `NOTIFY_MISC=false` (or `_JOB_POSTING`, `_INTERVIEW_INFO`) |
| Ping roles | `PING_ROLES=true` and `ROLE_JOB_POSTING=<role id>` |
| Daily "alive" ping | `HEARTBEAT_HOUR=9` |
| Keep a copy of every story | `ARCHIVE_DIR=/opt/story-watch/archive` (`ARCHIVE_MAX_MB=2048` caps it) |

## Pull the story archive

Needs `ARCHIVE_DIR` set and deployed. Stories only land in it from then on (the first run after enabling just seeds).

    PVE_HOST=root@pve CTID=120 .venv/bin/python scripts/pull_archive.py

Files go to `~/story-watch-data/archive/` (override with `ARCHIVE_PULL_DIR`). Re-run any time; it
only adds. Never commit it: the JSON has signed image URLs. The server deletes the oldest items past
`ARCHIVE_MAX_MB`, so pull now and then.

## Dev

    .venv/bin/pre-commit install                        # once: blocks commits with secrets
    .venv/bin/pytest
    .venv/bin/python scripts/resend_story.py --list     # see stories
    .venv/bin/python scripts/resend_story.py -n 1       # resend to TEST_DISCORD_WEBHOOK

## Don't

- Use your real Instagram account.
- Share the webhook URL or session file.
- Restart while rate-limited.
