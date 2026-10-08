# How users set their ping lists (issue #14)

Part of epic #3. This doc decides the interface that #16 builds. **Status: signed off by the user, 2026-10-08** (see [Decision](#decision-2026-10-08)).

## Decision (2026-10-08)

The user answered "yes" to every open question and added a scope change.

- **Option 1 is approved:** a slash-command bot over the gateway, no privileged intents, discord.py, and a second systemd unit sharing `state.db` in WAL mode. Posting stays on webhooks.
- **Bot apps:** the user creates the bot app plus a separate test app. `DISCORD_BOT_TOKEN` goes in `.env`.
- **No interim admin config file.** Option 3 is not built.
- **Scope change: one Discord server only.** From now on the project supports a single server. Preferences are simply per user. There is no "home server" and no guild↔webhook mapping, so the former questions 2 and 6 are moot. The research below was written for several servers; the parts that assumed that are marked **superseded** or rewritten.
- **Labeling channel:** the same bot will first host a labeling channel for #6. A separate branch is building it.

## TL;DR

Add a small **Discord bot that only handles slash commands** (`/pings edit`, `/pings show`, `/pings clear`). It connects to the gateway (outbound only), runs as a **second systemd unit** on CT 120, and writes preferences into the **same SQLite database** in WAL mode. **Posting stays on webhooks.** At send time the watcher reads the preferences and adds `<@user>` mentions with `allowed_mentions.users`. Preferences are **per user** (one server only, so there is no home server). The library is **discord.py 2.x**, run with no privileged intents. The token comes from `DISCORD_BOT_TOKEN` and is never logged.

## Where we are today

What the code does now, and what it means for this decision:

- `notify.py` posts through webhooks only. It pings roles with `allowed_mentions: {"parse": [], "roles": [...]}` and puts the mentions at the end of `content`. It has no way to receive anything from Discord.
- `config.py` defines a `Destination` for each server (`DISCORD_WEBHOOK`, `DISCORD_WEBHOOK_<n>`). Each one has its own `ROLE_<CATEGORY>[_<n>]` role IDs, and its key is the webhook id.
- `main.py` runs a synchronous loop in one thread. For each new item it posts to every destination and records each success in `deliveries`. The item is marked seen only after every server got it.
- `store.py` opens a single `sqlite3` connection with the default rollback journal and Python's default 5 s busy timeout. The schema is created by `CREATE TABLE IF NOT EXISTS` at startup.
- `deploy/` has one hardened unit (`ProtectSystem=strict`, `ReadWritePaths=/opt/story-watch/data ...`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `Restart=always`, `RestartSec=30`). It reads `EnvironmentFile=/opt/story-watch/.env`. `install.sh` is idempotent, and `push.sh` ships the workstation `.env` without its `TEST_*` lines.
- CT 120 has **no inbound path** from the internet. All Discord traffic is outbound HTTPS today.

So whatever lets users talk to us has to work over outbound connections, and it must not put the watcher's delivery guarantees at risk.

## Options compared

| | 1. Slash-command bot (gateway) | 2. Self-assigned roles + member-reading bot | 3. Admin-maintained config file | 4a. Web form over Tailscale | 4b. HTTP interactions endpoint via an outbound tunnel |
|---|---|---|---|---|---|
| Users change their own settings | Yes, in Discord | Yes, with Discord's own role UI (Onboarding / Channels & Roles, or reaction roles) | No. They ask an admin, who edits the file and redeploys | Only if they're on the tailnet | Yes, same UX as option 1 |
| "Don't ping me" list | Yes, stored explicitly | Awkward: needs a second role per tag (16 more roles per server) | Yes | Yes | Yes |
| Network | Outbound WSS + HTTPS only ✅ | Outbound only ✅ | None | Inbound over the tailnet; most users aren't on it ❌ | Public inbound HTTPS through a third-party tunnel ❌ (the issue says to avoid this) |
| Privileged intents | None | **GUILD_MEMBERS** (List Guild Members needs it) | n/a | n/a | None |
| Discord setup per server | Install the app once | Create about 16 to 32 roles per server, plus onboarding (onboarding needs a Community server) | None | None | Install the app once |
| Per-server vs. global | Either. Global is natural | Per-server by construction (roles are per guild) | Either | Either | Either |
| Identity / auth | Discord gives us the user id ✅ | Discord gives us the user id ✅ | Admin types ids | Needs Discord OAuth2 or trust on the tailnet ❌ | Discord gives us the user id, but we must verify Ed25519 signatures |
| New code | Bot unit, prefs tables, mentions in `notify.py` | Bot unit, member cache/sync, role→tag map per server, mentions | A parser plus mentions | Web app, auth, mentions | Web app, signature checks, tunnel, mentions |
| Discoverability | `/pings edit` lists every value with the current ones pre-selected | Native role pickers, but role names are the only docs | README | Form | Same as 1 |
| Failure isolation | Bot down = no edits, stories and pings still work | Bot down = stale member data | n/a | Web app down = no edits | Tunnel down = no edits |
| Verdict | **Recommended** | Second choice. More setup and a privileged intent for worse "don't ping" UX | Fine as a bootstrap or fallback only | Rejected | Rejected |

Notes on the options:

1. **Slash-command bot.** Interactions come in over the gateway when no Interactions Endpoint URL is set (the two methods are mutually exclusive [R1]). The connection is an outbound websocket, which fits CT 120. Replies are sent over HTTPS, which is also outbound. `INTERACTION_CREATE` isn't tied to any gateway intent [G1], so the bot can connect with `Intents.none()`.
2. **Roles.** Users pick roles in Onboarding or the Channels & Roles tab, which they can change later [S1]. Without a bot, the watcher could only ping tag roles, which is what `ROLE_<TYPE>` does today. Then "don't ping me" can't work, because any matching role pings. To get per-user matching, a bot has to list members and their roles, and `GET /guilds/{id}/members` requires the privileged GUILD_MEMBERS intent [D1]. Apps under 100 servers can turn that on in the portal without review [G1], so it's possible, just heavier. Each server would also need its own role→tag map, which means more env vars per `DISCORD_WEBHOOK_<n>`.
3. **Config file.** Something like `PING_USERS_FILE` (user id → ping list, mute list). It needs no bot. Every change takes an admin edit plus `push.sh`, and users can't see their own config. It's useful only if #16 wants to ship pings before the bot exists.
4. **Other ideas.**
   - (a) A **web form over Tailscale**. It fails because the people being pinged aren't on the tailnet, and we'd still need Discord OAuth2 to know who they are.
   - (b) An **HTTP interactions endpoint behind an outbound tunnel** (e.g. a Cloudflare-style tunnel). It technically avoids opening ports, but it makes a public endpoint reach into the home network and adds a third-party dependency. It brings nothing the gateway doesn't already give us.
   - (c) **Text commands** (e.g. `!pings`) need message content (privileged MESSAGE_CONTENT [G1]). They're worse UX than slash commands.
   - (d) **Hosted reaction-role bots** need our own member reader anyway, so they're option 2 with an extra third party.

## Answers to the issue's questions

### Per server or global?

**Superseded by the one-server scope (2026-10-08).** Preferences are simply per user. There is no home server and no guild mapping. The original multi-server analysis is kept below for the record.

<details><summary>Original multi-server analysis (superseded)</summary>

**Original recommendation: global preferences per user, with one "home server" where they get pinged.**

- The watcher posts the same story to every server. If a user is in servers 1 and 2 and we mention them in both, they get two pings for one story. That's the most annoying outcome we can design in.
- Tags describe the story, not the server. Someone who wants SWE new-grad postings wants them everywhere.
- **Home server** = the server where the user last ran `/pings edit` (the interaction carries `guild_id`). The watcher adds their mention only to the post in that server. `/pings show` says which server that is and how to move it ("run `/pings edit` in the other server").
- The watcher has to map guild id → `Destination`. Webhook ids are already the destination keys, and the webhook object has a `guild_id` field. `GET /webhooks/{id}/{token}` needs no auth [W1], so the watcher can resolve it once at startup and cache it in the DB. A failure there must not block posting; those users just aren't pinged until it resolves. **Not verified:** that the token variant of Get Webhook includes `guild_id`. The docs say only that it returns "no user". An explicit `DISCORD_GUILD_<n>` env var is the fallback.
- Per-server preferences would be a one-column change (`guild_id` in the primary key) if the user wants them later. The table layout in the migration sketch keeps that door open.

</details>

### Keep webhooks and add a bot only for commands, or move posting to the bot?

**Keep posting through webhooks. The bot handles commands only and posts nothing to channels.**

- The whole delivery path stays as it is: `deliveries` keyed by webhook id, retry without re-posting, one sender per `DISCORD_WEBHOOK_<n>`, and alerts on server 1 only. Moving posting to the bot would mean rewriting that path and giving the bot Send Messages / Embed Links in every story channel.
- A webhook message can ping users. Execute Webhook takes `allowed_mentions` [W1], the same field we already use for roles. #17 changes `Notifier.story` to put `<@id>` after the role mentions in `content` and to send `{"parse": [], "roles": [...], "users": [...]}`.
- The bot is then optional infrastructure. If it's down or its token is revoked, stories still post and saved pings still fire.
- The bot needs no channel permissions. Install it with scopes `bot applications.commands` and `permissions=0`. The docs say application commands don't depend on a bot user in the guild [C1]. **Not verified:** whether a gateway connection gets interactions from guilds where the app has only `applications.commands`. Installing with `bot` avoids the question.

### Bot token handling

- `DISCORD_BOT_TOKEN` goes in the same `/opt/story-watch/.env` (shipped by `push.sh`). Only the bot reads it; the watcher ignores it. Add it to `.env.example`, the README and `HUMANS.md` in #16.
- Same rule as webhook URLs: never log it, never put it in an exception message, and keep it out of `repr`. Run discord.py with `log_handler=None` and our own logging setup, and log exceptions by type only around login, the same as `notify.py` does for `requests`. **Not verified:** that discord.py never puts the token in its own log lines or exception text. #16 should add a test that runs a failed login against a mocked HTTP layer and asserts the token is absent from the captured logs.
- A missing token means the bot unit exits with a clear message. `install.sh` should enable the bot unit only when `DISCORD_BOT_TOKEN` is set, the same way it decides whether to start the watcher today.
- If the token leaks (e.g. in a commit, which gitleaks should catch), reset it in the Developer Portal and redeploy.
- **Restart storm risk:** Discord allows 1000 `IDENTIFY` calls per 24 h, and going over **terminates sessions and resets the bot token** [G1]. A crash-looping unit with today's `RestartSec=30` could restart 2880 times a day. The bot unit should use something like `RestartSec=120` plus `StartLimitIntervalSec`/`StartLimitBurst`, and let the library RESUME instead of re-identifying when it can.

### Discord limits that matter

| Limit | Value | Impact | Source |
|---|---|---|---|
| `allowed_mentions.users` / `.roles` | max 100 ids each | More than 100 matched users would need a second message. That's very unlikely here, but #17 should cap the list or split it | [M1] |
| Message `content` | 2000 chars (also for webhooks) | Each mention is about 21 to 22 chars. With the title, roughly 80 user mentions fit. #17 must truncate the title first and never cut a mention in half | [M1], [W1] |
| Ids in `allowed_mentions` but not in `content` | silently ignored | Harmless, but tests should check content and ids together | [M1] |
| Global REST rate limit | 50 req/s per bot. Interaction responses are exempt | Irrelevant at our volume | [L1] |
| Invalid requests | 10,000 per 10 min per IP (401/403/429), then a temporary IP ban | Shared by the watcher and the bot on one IP. Don't retry a 404'd webhook | [L1] |
| Webhook rate limits | No fixed number documented; read the headers | `notify.py` already honours `retry_after` on 429 | [L1] |
| Interaction response | within 3 s; token valid 15 min | DB writes must be quick (see SQLite below). Editor views time out after 15 min | [R1] |
| Command registration | 200 creates per day per guild; guild commands update instantly; global propagation time not stated | Sync only on deploy or on a flag, never on every start | [C1] |
| Commands | 100 global chat-input commands; 25 options/subcommands per command; 25 choices | One `/pings` group with 3 subcommands is far below this | [C1] |
| String select | ≤ 25 options, `max_values` ≤ 25, label ≤ 100 chars, `default` pre-selects | All 16 tag values fit in one select. If tags grow past 25, use one select per dimension | [X1] |
| Privileged intents | GUILD_MEMBERS, GUILD_PRESENCES, MESSAGE_CONTENT. Under 100 servers you can enable them yourself | The recommended bot needs none | [G1] |
| Gateway `IDENTIFY` | 1000 per 24 h, over the limit = token reset | See restart storm risk above | [G1] |
| Mentioning a user who isn't in that server | **Not verified** in the docs | Moot with one server, since users who set preferences are members | n/a |

### How users find the values they can pick and see their config

- `/pings edit` replies **ephemerally** (only the user sees it [R1]) with two multi-selects, "Ping me for" and "Never ping me for". They list every tag value as `Dimension: Value` (e.g. `Role: SWE`), with the user's current values pre-selected (`default` [X1]). Picking a value on one list removes it from the other. The reply text shows the saved state right away.
- `/pings show` prints both lists and a warning when the ping list is empty ("you won't be pinged").
- `/pings clear` deletes everything.
- Every reply links to `docs/tags.md` (#4) for what each value means, including how "Unknown" works under the epic's fail-open rule (#15 owns that).
- No free-text input, so there's nothing to validate beyond "is this a known value". The DB stores stable value ids. Labels come from the tag module, so renaming a label doesn't break saved preferences. Saved values that later disappear from the tag list are ignored and not shown.
- If the user prefers typed commands too, `/pings add <tag>` with autocomplete is easy to add later. Choices are capped at 25, autocomplete isn't [C1].

### Same process as the watcher, or a second unit sharing SQLite?

**A second systemd unit, `story-watch-bot.service`, sharing `/opt/story-watch/data/state.db`.**

- #16 requires that a bot disconnect not take down the watcher, and the reverse. Separate processes give us that for free. The watcher is a synchronous `threading` loop and discord.py is asyncio, so running them in one process would mean a second thread with its own event loop, plus shared crash handling.
- Both units use the same `storywatch` user, the same hardening and the same `ReadWritePaths`. The bot needs `AF_INET`/`AF_INET6` for the websocket, which is already allowed. **Nothing inbound is needed.**
- **SQLite concurrency, measured offline** (`research/ping-config-ui/sqlite_two_processes.py`, a fake watcher plus a fake `/pings` writer for 3 s each, with the watcher holding every write for an exaggerated 20 ms):
  - no busy timeout: 293 lock errors in the bot;
  - rollback journal with a 5 s timeout (today's `Store`): 0 errors, worst bot write 34 ms;
  - WAL with a 2 s timeout: 0 errors, worst bot write 34 ms, and readers never block on writers.

  Both are well inside the 3 s interaction deadline. I recommend WAL anyway (`PRAGMA journal_mode=WAL` once in `Store.__init__`; the setting persists in the file), so the watcher's per-story preference reads never wait on a bot write. The `-wal`/`-shm` files go in `data/`, which is already in `ReadWritePaths`. Debian 12's SQLite (3.40) supports WAL. These numbers were measured on the workstation (SQLite 3.53), not on CT 120.
- The bot should run its blocking `sqlite3` calls through `asyncio.to_thread`, so a locked database never stalls the gateway heartbeat.

## Library choice

| | discord.py | hikari |
|---|---|---|
| Latest checked | 2.7.1 | 2.6.0 |
| Python | `>=3.8`, classifiers include 3.11 ✅ [P1] | `>=3.10,<3.15`, classifiers include 3.11 ✅ [P2] |
| Slash commands built in | Yes (`app_commands`, `CommandTree`, `ui.Select`/`ui.View`, `ephemeral=`) [P3] | Core only. Commands usually come from an extra package (e.g. lightbulb, arc). **Not verified** in this research |
| Fit | One dependency, the most examples | Leaner, but needs a second library for commands |

**Pick discord.py.** It's the smallest dependency surface for this job. Pin it to `<3` in the service's `pyproject.toml` when #16 adds the bot.

## What the tag values come from

The values come from the dimensions in epic #3. #4 is defining them in `story_watch/tags.py`, so this doc doesn't depend on that file:

| Dimension | Values |
|---|---|
| Sponsorship | Sponsor or Canadian · No sponsor · Unknown |
| Post type | Event · Job posting · Process info · Misc |
| Company | FAANG+ · Quant · Other |
| Role | ML · SWE · PM · Other |
| Level | Internship · New grad |

That's 16 values, which fit in one 25-option select. Preferences store **stable value ids** (the sketch uses placeholders like `faang_plus` and `role_other`). The two "Other" values are different ids, so each value id has to be unique across all dimensions, or the DB has to store `(dimension, value)`. #16 imports the list from `tags.py` once #4 lands. How preferences combine with a story's tags (ping vs. mute precedence, Unknown, fail open) is #15's job.

## Recommendation and migration sketch (what #16 builds)

**Recommendation:** option 1. A discord.py gateway bot with `/pings edit|show|clear`, no privileged intents, as a second systemd unit sharing `state.db` (WAL). Preferences per user (one server only). Webhooks keep posting, and #17 adds `allowed_mentions.users`.

1. **Storage (`store.py`)**, created idempotently at startup, plus WAL:
   ```sql
   CREATE TABLE IF NOT EXISTS ping_users (
       user_id    TEXT PRIMARY KEY,  -- Discord user id
       updated_at REAL NOT NULL
   );
   CREATE TABLE IF NOT EXISTS ping_prefs (
       user_id TEXT NOT NULL REFERENCES ping_users(user_id) ON DELETE CASCADE,
       list    TEXT NOT NULL CHECK (list IN ('ping', 'mute')),
       tag     TEXT NOT NULL,        -- stable value id from tags.py
       PRIMARY KEY (user_id, tag)    -- a tag sits on one list at most
   );
   ```
   The earlier sketch also had `ping_users.home_guild` and a `guilds` table (webhook id → guild id). Both are dropped with the one-server scope.
2. **Bot (`story_watch/bot.py`, console script `story-watch-bot`)**: the shape is in `research/ping-config-ui/bot_sketch.py`. It reads `DISCORD_BOT_TOKEN`, runs `Intents.none()`, uses the `guild_only` `/pings` group and ephemeral replies, and sends DB calls through `asyncio.to_thread`. It runs a command sync only behind a `--sync` flag that `install.sh` passes once per deploy.
3. **Deploy**: `deploy/story-watch-bot.service`, a copy of the watcher's hardening with `ExecStart=.../story-watch-bot`, a longer `RestartSec` and a start limit. `install.sh` installs it every time and enables/starts it only when `DISCORD_BOT_TOKEN` is set, so the script stays idempotent.
4. **Config/docs**: `DISCORD_BOT_TOKEN` goes in `.env.example`, the README and `HUMANS.md`. `HUMANS.md` also covers creating the app, the install URL (`scope=bot+applications.commands&permissions=0`) and resetting the token.
5. **Tests**: mock discord.py's interaction objects and the DB. Cover idempotent migration, moving a tag between lists, unknown-value rejection, and the token never appearing in logs. No network.
6. **#17 (not #16)**: when posting, read the matching users with #15's rules, append `<@id>` after the role mentions, and add `users` to `allowed_mentions` (≤ 100, within 2000 chars).

**Bootstrap fallback (not used):** the user decided against an interim admin config file, so option 3 is not built.

## Open questions (all resolved, 2026-10-08)

The user said yes to all of these.

| # | Question | Outcome |
|---|---|---|
| 1 | Approve option 1 (slash-command bot over the gateway, no privileged intents)? | **Yes** |
| 2 | Global preferences + home server, or per-server? | **Moot.** One server only; preferences are per user |
| 3 | Keep posting through webhooks, bot handles commands only? | **Yes** |
| 4 | Second systemd unit sharing `state.db`, DB switched to WAL? | **Yes** |
| 5 | discord.py as a new runtime dependency? | **Yes** |
| 6 | Guild-id mapping (webhook lookup vs. `DISCORD_GUILD_<n>`)? | **Moot.** No guild mapping with one server |
| 7 | User creates the bot app and a separate test app; `DISCORD_BOT_TOKEN` in `.env`? | **Yes** |
| 8 | Interim config file before the bot ships? | **No** |

## Prototype

`research/ping-config-ui/` (throwaway; its dependencies stay out of `pyproject.toml`):

- `sqlite_two_processes.py` was **run offline** (stdlib only). It produced the numbers above.
- `bot_sketch.py` was **not executed**. There were no Discord credentials or test server, and discord.py isn't installed. It was only checked with `py_compile`. Run it only against a test app and a test server.

## Sources

Read on 2026-10-08. The old `discord.com/developers/docs/...` URLs now redirect to `docs.discord.com/developers/...`.

- [R1] Receiving and responding to interactions: https://docs.discord.com/developers/interactions/receiving-and-responding
- [C1] Application commands: https://docs.discord.com/developers/interactions/application-commands
- [G1] Gateway (intents, privileged intents, identify limit): https://docs.discord.com/developers/events/gateway
- [M1] Message resource (allowed mentions, content length): https://docs.discord.com/developers/resources/message
- [W1] Webhook resource (Execute Webhook, Get Webhook with Token): https://docs.discord.com/developers/resources/webhook
- [D1] Guild resource (List Guild Members needs GUILD_MEMBERS): https://docs.discord.com/developers/resources/guild
- [L1] Rate limits: https://docs.discord.com/developers/topics/rate-limits
- [X1] Component reference (string select): https://docs.discord.com/developers/components/reference
- [S1] Discord support, Community Onboarding (read through a search summary, not fetched directly): https://support.discord.com/hc/en-us/articles/11074987197975
- [P1] discord.py on PyPI: https://pypi.org/project/discord.py/
- [P2] hikari on PyPI: https://pypi.org/project/hikari/
- [P3] discord.py interactions API reference: https://discordpy.readthedocs.io/en/stable/interactions/api.html

### Not verified

- How Discord treats a mention of a user who isn't a member of that server.
- Whether Get Webhook with Token returns `guild_id`.
- Whether gateway interactions arrive from guilds where the app has only `applications.commands`.
- That discord.py never logs the token.
- How long global command propagation takes.
- hikari's command-handler story.
- The legacy limit of 5 action rows per message. The reference page I read only gives the 40-component limit for Components V2. Two selects are far below either limit.
