"""Throwaway sketch for issue #14: the /pings editor as a discord.py gateway bot.

NOT EXECUTED. No Discord credentials or test server existed during the research,
and discord.py isn't installed in the service venv (see requirements.txt). It
was only checked with `python -m py_compile`. It shows the shape #16 would build,
not production code:

- gateway only (outbound), no privileged intents, posts nothing to channels;
- `/pings edit` replies with an ephemeral message holding two multi-selects
  ("ping me", "don't ping me") pre-filled with the user's current values;
- `/pings show` and `/pings clear`;
- preferences are global per user, plus the server they were last saved from
  ("home"), which is where the watcher would ping them.

The tag values are placeholders copied from epic #3. #16 should import them
from story_watch/tags.py (#4) instead.

    DISCORD_BOT_TOKEN=... python bot_sketch.py [--sync-guild <id> | --sync-global]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time

import discord
from discord import app_commands

log = logging.getLogger("ping-bot-sketch")

# (dimension, stable value, label) from the table in epic #3; placeholder for tags.py.
TAGS: list[tuple[str, str, str]] = [
    ("Sponsorship", "sponsor_or_canadian", "Sponsor or Canadian"),
    ("Sponsorship", "no_sponsor", "No sponsor"),
    ("Sponsorship", "sponsorship_unknown", "Unknown"),
    ("Post type", "event", "Event"),
    ("Post type", "job_posting", "Job posting"),
    ("Post type", "process_info", "Process info"),
    ("Post type", "misc", "Misc"),
    ("Company", "faang_plus", "FAANG+"),
    ("Company", "quant", "Quant"),
    ("Company", "company_other", "Other"),
    ("Role", "ml", "ML"),
    ("Role", "swe", "SWE"),
    ("Role", "pm", "PM"),
    ("Role", "role_other", "Other"),
    ("Level", "internship", "Internship"),
    ("Level", "new_grad", "New grad"),
]
LABELS = {value: f"{dim}: {label}" for dim, value, label in TAGS}
LISTS = {"ping": "Ping me for", "mute": "Never ping me for"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ping_users (
    user_id    TEXT PRIMARY KEY,  -- Discord user id (global across servers)
    home_guild TEXT NOT NULL,     -- server the user is pinged in
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ping_prefs (
    user_id TEXT NOT NULL REFERENCES ping_users(user_id) ON DELETE CASCADE,
    list    TEXT NOT NULL CHECK (list IN ('ping', 'mute')),
    tag     TEXT NOT NULL,        -- stable value from tags.py
    PRIMARY KEY (user_id, tag)    -- a tag sits on at most one list
);
"""


class Prefs:
    """Tiny synchronous store; every call is sub-millisecond, run off the event loop anyway."""

    def __init__(self, path: str):
        # 2 s busy timeout keeps a locked database well inside the 3 s interaction deadline.
        self._db = sqlite3.connect(path, timeout=2.0, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def get(self, user_id: int) -> dict[str, set[str]]:
        rows = self._db.execute("SELECT list, tag FROM ping_prefs WHERE user_id = ?", (str(user_id),))
        out: dict[str, set[str]] = {"ping": set(), "mute": set()}
        for lst, tag in rows:
            if tag in LABELS:  # values retired from tags.py are ignored, not shown as valid
                out[lst].add(tag)
        return out

    def home(self, user_id: int) -> str | None:
        row = self._db.execute("SELECT home_guild FROM ping_users WHERE user_id = ?", (str(user_id),)).fetchone()
        return row[0] if row else None

    def set_list(self, user_id: int, guild_id: int, lst: str, tags: set[str]) -> None:
        bad = tags - LABELS.keys()
        if bad:
            raise ValueError(f"unknown tags: {sorted(bad)}")
        with self._db:
            self._db.execute(
                "INSERT INTO ping_users (user_id, home_guild, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (user_id) DO UPDATE SET home_guild = excluded.home_guild,"
                " updated_at = excluded.updated_at",
                (str(user_id), str(guild_id), time.time()),
            )
            self._db.execute("DELETE FROM ping_prefs WHERE user_id = ? AND list = ?", (str(user_id), lst))
            # A tag picked here moves off the other list (PRIMARY KEY (user_id, tag)).
            self._db.executemany(
                "INSERT OR REPLACE INTO ping_prefs (user_id, list, tag) VALUES (?, ?, ?)",
                [(str(user_id), lst, t) for t in sorted(tags)],
            )

    def clear(self, user_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM ping_users WHERE user_id = ?", (str(user_id),))


def summary(prefs: dict[str, set[str]], home: str | None, here: int | None) -> str:
    lines = []
    for lst, title in LISTS.items():
        tags = ", ".join(LABELS[t] for t in sorted(prefs[lst], key=list(LABELS).index)) or "nothing"
        lines.append(f"**{title}:** {tags}")
    if not prefs["ping"]:
        lines.append("Your ping list is empty, so you won't be pinged.")
    elif home is not None and here is not None and home != str(here):
        lines.append("You're pinged in another server. Saving here moves your pings to this server.")
    lines.append("What the tags mean: see docs/tags.md in the repo.")
    return "\n".join(lines)


class ListSelect(discord.ui.Select):
    def __init__(self, store: Prefs, lst: str, current: set[str]):
        super().__init__(
            placeholder=LISTS[lst],
            min_values=0,
            max_values=len(TAGS),  # 16 values, under the 25-option cap
            options=[discord.SelectOption(label=LABELS[v], value=v, default=v in current) for _, v, _ in TAGS],
        )
        self.store, self.lst = store, lst

    async def callback(self, interaction: discord.Interaction) -> None:
        uid, gid = interaction.user.id, interaction.guild_id
        await asyncio.to_thread(self.store.set_list, uid, gid, self.lst, set(self.values))
        prefs = await asyncio.to_thread(self.store.get, uid)
        # Rebuild both selects so a tag moved between lists shows up right away.
        await interaction.response.edit_message(
            content=summary(prefs, str(gid), gid), view=Editor(self.store, prefs)
        )


class Editor(discord.ui.View):
    def __init__(self, store: Prefs, prefs: dict[str, set[str]]):
        super().__init__(timeout=900)  # the interaction token behind it lasts 15 minutes
        for lst in LISTS:
            self.add_item(ListSelect(store, lst, prefs[lst]))


class PingBot(discord.Client):
    def __init__(self, store: Prefs, sync_guild: int | None, sync_global: bool = False):
        super().__init__(intents=discord.Intents.none())  # slash commands need no intents
        self.tree = app_commands.CommandTree(self)
        self.store, self.sync_guild, self.sync_global = store, sync_guild, sync_global
        self.tree.add_command(self._group())

    def _group(self) -> app_commands.Group:
        store = self.store
        group = app_commands.Group(name="pings", description="Choose which stories ping you", guild_only=True)

        @group.command(name="edit", description="Pick the tags that ping you, or never ping you")
        async def edit(interaction: discord.Interaction) -> None:
            prefs = await asyncio.to_thread(store.get, interaction.user.id)
            home = await asyncio.to_thread(store.home, interaction.user.id)
            await interaction.response.send_message(
                summary(prefs, home, interaction.guild_id), view=Editor(store, prefs), ephemeral=True
            )

        @group.command(name="show", description="Show your current ping settings")
        async def show(interaction: discord.Interaction) -> None:
            prefs = await asyncio.to_thread(store.get, interaction.user.id)
            home = await asyncio.to_thread(store.home, interaction.user.id)
            await interaction.response.send_message(summary(prefs, home, interaction.guild_id), ephemeral=True)

        @group.command(name="clear", description="Stop all pings and forget your settings")
        async def clear(interaction: discord.Interaction) -> None:
            await asyncio.to_thread(store.clear, interaction.user.id)
            await interaction.response.send_message("Cleared. You won't be pinged.", ephemeral=True)

        return group

    async def setup_hook(self) -> None:
        # Sync only when asked (e.g. once per deploy), not on every start: command
        # creates are limited to 200 per day per guild. A test guild updates instantly;
        # --sync-global registers once for every server the app is installed in.
        if self.sync_guild is not None:
            guild = discord.Object(id=self.sync_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("synced commands to one guild")
        elif self.sync_global:
            await self.tree.sync()
            log.info("synced global commands")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="prefs-sketch.db")
    parser.add_argument("--sync-guild", type=int, help="register the commands in this test guild")
    parser.add_argument("--sync-global", action="store_true", help="register the commands globally")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_BOT_TOKEN is not set")  # never log the value itself
        return 2
    # log_handler=None: keep our logging config instead of discord.py's default handler.
    PingBot(Prefs(args.db), args.sync_guild, args.sync_global).run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
