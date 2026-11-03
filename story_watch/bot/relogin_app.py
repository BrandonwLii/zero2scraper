"""Discord side of the re-login buttons: two persistent buttons (they survive restarts) and
/relogin. Owner only (ALERT_USER_ID). All logic is in relogin.py."""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands, ui

from . import relogin as R

log = logging.getLogger(__name__)

LABELS = {"start": ("Start login browser", discord.ButtonStyle.primary), "done": ("I've logged in", discord.ButtonStyle.success)}


class ReloginButton(ui.DynamicItem[ui.Button], template=r"swrelogin:(?P<action>start|done)"):
    def __init__(self, action: str):
        label, style = LABELS[action]
        super().__init__(ui.Button(label=label, style=style, custom_id=f"swrelogin:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match: re.Match):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle(interaction, self.action)


def relogin_view() -> ui.View:
    view = ui.View(timeout=None)
    for action in R.ACTIONS:
        view.add_item(ReloginButton(action))
    return view


async def handle(interaction: discord.Interaction, action: str) -> None:
    cfg = interaction.client.cfg  # type: ignore[attr-defined]
    if not R.is_owner(interaction.user.id, cfg.alert_user_id):
        await interaction.response.send_message(R.NOT_OWNER, ephemeral=True)
        return
    if cfg.relogin_dir is None:
        await interaction.response.send_message(R.DISABLED, ephemeral=True)
        return
    result = await asyncio.to_thread(R.write_request, cfg.relogin_dir, action)
    if result == "busy":
        text = R.BUSY
    elif result != "ok":
        log.warning("could not write the re-login request")
        text = R.FAILED_WRITE
    else:
        text = R.start_reply(cfg.relogin_url) if action == "start" else R.DONE_REPLY
    await interaction.response.send_message(text, ephemeral=True)


def add_command(tree: app_commands.CommandTree, guild_id: int) -> None:
    @app_commands.command(name="relogin", description="Refresh the Instagram login from your phone (owner only)")
    async def relogin(interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg  # type: ignore[attr-defined]
        if not R.is_owner(interaction.user.id, cfg.alert_user_id):
            await interaction.response.send_message(R.NOT_OWNER, ephemeral=True)
            return
        if cfg.relogin_dir is None:
            await interaction.response.send_message(R.DISABLED, ephemeral=True)
            return
        await interaction.response.send_message(
            "Press **Start login browser**, log in, then press **I've logged in**.", view=relogin_view(), ephemeral=True
        )

    tree.add_command(relogin, guild=discord.Object(id=guild_id))
