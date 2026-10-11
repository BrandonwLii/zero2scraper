"""The /nopost slash commands: the server-wide "no post" list (#13).

/nopost show  the list (anyone in the server; it is read-only)
/nopost edit  labelers only: add a rule from a multi-select, remove one, or reset the list

Every reply is ephemeral. The list lives in the watcher's SQLite DB (Store.*_nopost_rules); the
watcher reads it every cycle and applies it on top of NOTIFY_* (story_watch/nopost.py). The command
is added to the one server's tree here and published by the sync in pings_app.register.

Same conventions as pings_app: blocking sqlite calls go through asyncio.to_thread with a
short-lived Store, and failures are logged by exception type only.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands, ui

from ..nopost import MAX_RULES, NoPostRule, RuleError
from ..store import Store
from . import nopost as N

log = logging.getLogger(__name__)

FAILED = "Could not reach the state database (see the bot log). Nothing was changed."
NOT_ALLOWED = "Only labelers can change the no-post list. `/nopost show` lets you read it."


def _with_store(db_path: Path, fn: Callable[[Store], object]):
    store = Store(db_path)
    try:
        return fn(store)
    finally:
        store.close()


def _load(db_path: Path) -> list[tuple[int, NoPostRule]]:
    return _with_store(db_path, lambda s: s.get_nopost_rules())  # type: ignore[return-value]


def _add(db_path: Path, rule: NoPostRule) -> bool:
    return _with_store(db_path, lambda s: s.add_nopost_rule(rule)) is not None


def _remove(db_path: Path, rule_id: int) -> bool:
    return bool(_with_store(db_path, lambda s: s.remove_nopost_rule(rule_id)))


def _reset(db_path: Path) -> int:
    return _with_store(db_path, lambda s: s.clear_nopost_rules())  # type: ignore[return-value]


def edit_text(rules: list[tuple[int, NoPostRule]], picks: list[str], note: str = "") -> str:
    parts = [N.EDIT_INTRO, "", N.describe(rules, editable=True)]
    if picks:
        rule, why = N.build_rule(picks)
        parts += ["", f"**New rule:** {rule.describe()}" if rule else f"**New rule (can't be added yet):** {why}"]
    if note:
        parts += ["", note]
    return N.fit("\n".join(parts))


class ValuesSelect(ui.Select):
    def __init__(self, picks: list[str]):
        super().__init__(
            placeholder="Values for a new rule",
            min_values=0,
            max_values=len(N.all_options()),
            options=[discord.SelectOption(label=lab, value=key, default=key in picks) for key, lab in N.all_options()],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: EditView = self.view  # type: ignore[assignment]
        view.picks = sorted(self.values)
        view.armed = False
        await view.refresh(interaction)


class AddButton(ui.Button):
    def __init__(self):
        super().__init__(label="Add rule", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: EditView = self.view  # type: ignore[assignment]
        view.armed = False
        rule, why = N.build_rule(view.picks)
        if rule is None:
            await view.refresh(interaction, why or "Pick at least one value.")
            return
        if len(view.rules) >= MAX_RULES:
            await view.refresh(interaction, f"The list is full ({MAX_RULES} rules). Remove one first.")
            return
        try:
            added = await asyncio.to_thread(_add, view.db_path, rule)
        except RuleError as e:  # the list filled up since this view was drawn
            await view.refresh(interaction, str(e))
            return
        except Exception as e:
            log.error("could not save a no-post rule (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        if added:
            log.info("no-post rule added by a labeler: %s", " + ".join(rule.to_keys()))
        view.picks = []
        await view.refresh(interaction, "Added." if added else "That rule is already on the list.")


class ResetButton(ui.Button):
    """Two presses: the first arms it, so one stray click doesn't empty the list."""

    def __init__(self, armed: bool):
        super().__init__(
            label="Press again to reset the list" if armed else "Reset list",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        self.armed = armed

    async def callback(self, interaction: discord.Interaction) -> None:
        view: EditView = self.view  # type: ignore[assignment]
        if not self.armed:
            view.armed = True
            await view.refresh(interaction)
            return
        try:
            n = await asyncio.to_thread(_reset, view.db_path)
        except Exception as e:
            log.error("could not reset the no-post list (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        log.info("no-post list reset by a labeler (%d rule(s))", n)
        view.armed = False
        await view.refresh(interaction, f"Reset. Removed {n} rule(s); every story is posted.")


class RemoveSelect(ui.Select):
    def __init__(self, rules: list[tuple[int, NoPostRule]]):
        super().__init__(
            placeholder="Remove a rule",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=N.rule_label(rid, r), value=str(rid)) for rid, r in rules[:MAX_RULES]],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: EditView = self.view  # type: ignore[assignment]
        view.armed = False
        try:
            gone = await asyncio.to_thread(_remove, view.db_path, int(self.values[0]))
        except Exception as e:
            log.error("could not remove a no-post rule (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        await view.refresh(interaction, "Removed." if gone else "That rule was already gone.")


class EditView(ui.View):
    def __init__(
        self,
        db_path: Path,
        labeler_ids: frozenset[int],
        rules: list[tuple[int, NoPostRule]],
        picks: list[str] | None = None,
        armed: bool = False,
    ):
        super().__init__(timeout=900)  # the interaction token dies after 15 minutes anyway
        self.db_path = db_path
        self.labeler_ids = labeler_ids
        self.rules = rules
        self.picks = picks or []
        self.armed = armed
        self.add_item(ValuesSelect(self.picks))
        self.add_item(AddButton())
        self.add_item(ResetButton(armed))
        if rules:
            self.add_item(RemoveSelect(rules))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.labeler_ids:
            return True
        await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
        return False

    async def refresh(self, interaction: discord.Interaction, note: str = "") -> None:
        """Re-read the list and redraw the message with a fresh view."""
        try:
            rules = await asyncio.to_thread(_load, self.db_path)
        except Exception as e:
            log.error("could not read the no-post list (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        self.stop()
        fresh = EditView(self.db_path, self.labeler_ids, rules, self.picks, self.armed)
        text = edit_text(rules, self.picks, note or ("Press Reset list again to confirm." if self.armed else ""))
        await interaction.response.edit_message(content=text, view=fresh)


class NoPostGroup(app_commands.Group):
    def __init__(self, db_path: Path, labeler_ids: frozenset[int]):
        super().__init__(name="nopost", description="Stories the watcher never posts", guild_only=True)
        self.db_path = db_path
        self.labeler_ids = labeler_ids

    @app_commands.command(name="show", description="Show the no-post list")
    async def show(self, interaction: discord.Interaction) -> None:
        try:
            rules = await asyncio.to_thread(_load, self.db_path)
        except Exception as e:
            log.error("could not read the no-post list (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        editable = interaction.user.id in self.labeler_ids
        text = N.describe(rules, editable=editable)
        if editable:
            text += "\nRun `/nopost edit` to change it."
        await interaction.response.send_message(N.fit(text), ephemeral=True)

    @app_commands.command(name="edit", description="Change the no-post list (labelers only)")
    async def edit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in self.labeler_ids:
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        try:
            rules = await asyncio.to_thread(_load, self.db_path)
        except Exception as e:
            log.error("could not read the no-post list (%s)", type(e).__name__)
            await interaction.response.send_message(FAILED, ephemeral=True)
            return
        await interaction.response.send_message(
            edit_text(rules, []), view=EditView(self.db_path, self.labeler_ids, rules), ephemeral=True
        )


def add_command(tree: app_commands.CommandTree, db_path: Path, labeler_ids: frozenset[int], guild_id: int) -> None:
    """Add /nopost for the one server. The caller syncs (pings_app.register does)."""
    tree.add_command(NoPostGroup(db_path, labeler_ids), guild=discord.Object(id=guild_id))
