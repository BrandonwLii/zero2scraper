# story-watch, the short version

Posts new Instagram stories to Discord. Runs in CT 120 on Proxmox.
Full details in README.md.

## Daily stuff

Logs:

    ssh root@<proxmox> pct exec 120 -- journalctl -u story-watch -n 100 --no-pager

Deploy a new version:

    PVE_HOST=root@<proxmox> CTID=120 ./deploy/push.sh

When it's done, Discord gets "story-watch deployed" with the commit id. No
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

       scp ~/.config/instaloader/session-<burner> root@<proxmox>:/root/
       ssh root@<proxmox> 'pct push 120 /root/session-<burner> /opt/story-watch/.config/instaloader/session-<burner> --user storywatch --group storywatch --perms 0600 && rm /root/session-<burner>'

No restart needed. You'll get "recovered" in Discord.

## Common .env changes

| Want to | Set |
|---|---|
| Watch someone | `TARGETS=user1,user2:<userid>` (userid avoids 429s) |
| Mute a post type | `NOTIFY_MISC=false` (or `_JOB_POSTING`, `_INTERVIEW_INFO`) |
| Ping roles | `PING_ROLES=true` and `ROLE_JOB_POSTING=<role id>` |
| Daily "alive" ping | `HEARTBEAT_HOUR=9` |

## Dev

    .venv/bin/pytest
    .venv/bin/python scripts/resend_story.py --list     # see stories
    .venv/bin/python scripts/resend_story.py -n 1       # resend to TEST_DISCORD_WEBHOOK

## Don't

- Use your real Instagram account.
- Share the webhook URL or session file.
- Restart while rate-limited.
