"""The /pings slash commands: each user sets who-they-want-pinged-for lists (#16).

/pings edit   two multi-selects ("Ping me for", "Never ping me for"); each pick saves right away
/pings show   the saved lists, with warnings
/pings clear  delete everything saved

Every reply is ephemeral. Preferences are stored per Discord user in the watcher's SQLite DB
(Store.set_ping_prefs); pinging itself is #17 and happens in the watcher, not here. Commands are
registered for the one server only (guild sync), so they show up instantly.

Blocking sqlite calls go through asyncio.to_thread with a short-lived Store, because a
connection can't be shared between threads. Failures are logged by exception type only.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands, ui

from ..pings import PingPrefs
from ..store import Store
from . import prefs as P

log = logging.getLogger(__name__)

EDIT_INTRO = "**Your pings.** Changes save as you pick. A value moves off the other list when you pick it here."
FAILED = "Could not reach the preferences database (see the bot log). Nothing was changed."


def _load(db_path: Path, user_id: str) -> PingPrefs | None:
    store = Store(db_path)
    try:
        return store.get_ping_prefs(user_id)
    finally:
        store.close()


def _save(db_path: Path, user_id: str, prefs: PingPrefs) -> None:
    store = Store(db_path)
    try:
        store.set_ping_prefs(user_id, prefs)
    finally:
        store.close()


def _clear(db_path: Path, user_id: str) -> bool:
    store = Store(db_path)
    try:
        return store.clear_ping_prefs(user_id)
    finally:
        store.close()


def edit_text(ping: list[str], mute: list[str], saved: bool) -> str:
    return f"{EDIT_INTRO}\n\n{P.describe(PingPrefs.from_keys(ping, mute), saved)}\n\n{P.help_footer()}"


class PrefsSelect(ui.Select):
    def __init__(self, placeholder: str, selected: list[str], is_ping: bool):
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(P.all_options()),
            options=[discord.SelectOption(label=lab, value=key, default=key in selected) for key, lab in P.all_options()],
        )
        self.is_ping = is_ping

    async def callback(self, interaction: discord.Interaction) -> None:
        view: EditView = self.view  # type: ignore[assignment]
        pick = P.pick_ping if self.is_ping else P.pick_mute
        ping, mute = pick(view.ping, view.mute, self.values)
        try:
            await asyncio.to_thread(_save, view.db_path, view.user_id, PingPrefs.from_keys(ping, mute))
        except Exception as e:
            log.error("could not save ping preferences (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        view.stop()
        fresh = EditView(view.db_path, view.user_id, ping, mute)
        await interaction.response.edit_message(content=edit_text(ping, mute, True), view=fresh)


class EditView(ui.View):
    def __init__(self, db_path: Path, user_id: str, ping: list[str], mute: list[str]):
        super().__init__(timeout=900)  # the interaction token dies after 15 minutes anyway
        self.db_path = db_path
        self.user_id = user_id
        self.ping = ping
        self.mute = mute
        self.add_item(PrefsSelect("Ping me for", ping, True))
        self.add_item(PrefsSelect("Never ping me for", mute, False))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id


class PingsGroup(app_commands.Group):
    def __init__(self, db_path: Path):
        super().__init__(name="pings", description="Choose which stories ping you", guild_only=True)
        self.db_path = db_path

    @app_commands.command(name="edit", description="Choose what to be pinged for")
    async def edit(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)
        try:
            saved = await asyncio.to_thread(_load, self.db_path, user_id)
        except Exception as e:
            log.error("could not read ping preferences (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        ping, mute = P.initial_lists(saved)
        await interaction.response.send_message(
            edit_text(ping, mute, saved is not None),
            view=EditView(self.db_path, user_id, ping, mute),
            ephemeral=True,
        )

    @app_commands.command(name="show", description="Show what you are pinged for")
    async def show(self, interaction: discord.Interaction) -> None:
        try:
            saved = await asyncio.to_thread(_load, self.db_path, str(interaction.user.id))
        except Exception as e:
            log.error("could not read ping preferences (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        if saved is None:
            text = "**You won't be pinged.** You haven't saved anything. Run `/pings edit` to choose."
        else:
            text = P.describe(saved)
        await interaction.response.send_message(f"{text}\n\n{P.help_footer()}", ephemeral=True)

    @app_commands.command(name="clear", description="Delete all your ping settings")
    async def clear(self, interaction: discord.Interaction) -> None:
        try:
            had = await asyncio.to_thread(_clear, self.db_path, str(interaction.user.id))
        except Exception as e:
            log.error("could not clear ping preferences (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        msg = "Cleared. You won't be pinged." if had else "You had nothing saved. You won't be pinged."
        await interaction.response.send_message(msg, ephemeral=True)


async def register(client: discord.Client, tree: app_commands.CommandTree, db_path: Path, guild_id: int) -> None:
    """Add /pings for the one server and sync it (guild commands update instantly).

    Syncing on every start is cheap: starts are limited by the unit (5 per hour at most)."""
    guild = discord.Object(id=guild_id)
    tree.add_command(PingsGroup(db_path), guild=guild)
    await tree.sync(guild=guild)
    log.info("registered /pings")
