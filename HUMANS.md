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
| Post to another server | `DISCORD_WEBHOOK_2=<url>` and `ROLE_JOB_POSTING_2=<that server's role id>` |
| Daily "alive" ping | `HEARTBEAT_HOUR=9` |
| Keep a copy of every story | `ARCHIVE_DIR=/opt/story-watch/archive` (`ARCHIVE_MAX_MB=2048` caps it) |

## Pull the story archive

Needs `ARCHIVE_DIR` set and deployed. Stories only land in it from then on (the first run after enabling just seeds).

    PVE_HOST=root@pve CTID=120 .venv/bin/python scripts/pull_archive.py

Files go to `~/story-watch-data/archive/` (override with `ARCHIVE_PULL_DIR`). Re-run any time; it
only adds. Never commit it: the JSON has signed image URLs. The server deletes the oldest items past
`ARCHIVE_MAX_MB`, so pull now and then.

## Labeling bot setup

One-time, per Discord server. You label stories in a private channel; the answers become the ground truth for scoring taggers.

1. Discord Developer Portal (https://discord.com/developers/applications) -> **New Application**, name it.
2. **Bot** tab -> leave all three **Privileged Gateway Intents** off. Click **Reset Token** and copy it once. It goes only in `.env` as `DISCORD_BOT_TOKEN`; never paste it in chat, issues or commits. If it leaks, reset it here.
3. **OAuth2 -> URL Generator**: tick the scope `bot` and nothing else. Under *Bot Permissions* tick only View Channel, Send Messages, Attach Files, Read Message History and Embed Links. Open the generated URL and add the bot to your server.
4. Make a private channel (e.g. `#story-labels`), visible only to you, the people who may label and the bot. If the invite gave the bot server-wide permissions, remove them and grant those five permissions on this channel only.
5. Discord **Settings -> Advanced -> Developer Mode**. Right-click the channel -> **Copy Channel ID**. Right-click each person who may label -> **Copy User ID**.
6. Put in `.env` (and `ARCHIVE_DIR` must already be set):

       DISCORD_BOT_TOKEN=<token>
       LABEL_CHANNEL_ID=<channel id>
       LABELER_USER_IDS=<your id>,<friend's id>

7. Deploy as usual (`PVE_HOST=root@pve CTID=120 ./deploy/push.sh`). `install.sh` starts `story-watch-bot` only when the token is set. First start posts the newest 10 archived stories; set `LABEL_BACKLOG_MAX` or `LABEL_SINCE` to change that.
8. Check: `ssh root@pve pct exec 120 -- journalctl -u story-watch-bot -n 50 --no-pager` should say "connected to Discord".

Using it: press **Label** under a story, pick values, **Submit**, check the private preview, optionally **Add note**, then **Save**. Nothing is saved before Save. **Relabel** changes a label later. Pull the labels with the archive (`labels.jsonl` is in it).

**A separate test bot for development.** Make a second application and bot the same way, invite it to a throwaway server with its own private channel, and use its token and ids in your local `.env`. Run `.venv/bin/story-watch-bot` on the workstation against a scratch `ARCHIVE_DIR` (a copy of a pulled archive). Never run two copies with the same token, and don't point a dev copy at the production archive.

If the bot exits with "config error" or "rejected the bot token" it will not restart by itself; fix `.env`, redeploy, or `systemctl reset-failed story-watch-bot && systemctl start story-watch-bot` in the CT.

## Dev

    .venv/bin/pre-commit install                        # once: blocks commits with secrets
    .venv/bin/pytest
    .venv/bin/python scripts/resend_story.py --list     # see stories
    .venv/bin/python scripts/resend_story.py -n 1       # resend to TEST_DISCORD_WEBHOOK

## Don't

- Use your real Instagram account.
- Share the webhook URL or session file.
- Restart while rate-limited.
