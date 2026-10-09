"""The Discord side of the labeler: posting stories and collecting labels.

Flow. Each story is posted once in the label channel with a "Label" button. The button opens a
modal with one multi-select per tag dimension (a modal holds at most 5 components and there are
5 dimensions). Submitting the modal does not save anything: the labeler gets a private preview
with Save, Add note and Edit tags buttons, and only Save writes the label. The channel message is
then edited to show the saved labels and who saved them.

Restarts. The channel button is a DynamicItem whose custom_id carries the media id, so it keeps
working after a restart without any per-message bookkeeping. The private preview is not
persistent: if the bot restarts mid-label, the labeler just presses Label again.

Nothing here logs the token, and failures are logged by exception type only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from urllib.parse import urlsplit

import discord
from discord import app_commands, ui

from ..tags import DIMENSIONS
from .config import BotConfig
from .labels import NOTE_MAX, LabelError, append_label, ignored_dimensions, is_allowed, make_label, read_labels
from .queue import PostedLog, Sidecar, choose_media, find_sidecar, scan_archive, select_new
from . import pings_app
from .render import (
    DIMENSION_TITLES,
    describe_selection,
    describe_tags,
    guess_selection,
    story_description,
)

log = logging.getLogger(__name__)

MAX_POSTS_PER_SCAN = 25
EMBED_COLOR = 0x5865F2
SAVED_COLOR = 0x2ECC71
REFUSAL = "You're not on the list of people allowed to label stories."


def intents() -> discord.Intents:
    """No privileged intents. `guilds` (not privileged) gives the server's upload limit."""
    out = discord.Intents.none()
    out.guilds = True
    return out


# -- state shared by the callbacks -------------------------------------------------------------


@dataclass
class Draft:
    """An unsaved label. Lives only in memory until Save."""

    media_id: str
    target: str
    selection: dict[str, list[str]]
    note: str = ""
    message: discord.Message | None = None  # the story post to update after saving


class LabelState:
    """Config plus the one file the labels are written to; handed to every callback."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def allowed(self, user_id: int) -> bool:
        return is_allowed(user_id, self.cfg.labeler_ids)


# -- the story post ------------------------------------------------------------------------------


def story_embed(sc: Sidecar, note: str = "") -> discord.Embed:
    embed = discord.Embed(title="New story", description=story_description(sc), color=EMBED_COLOR)
    embed.timestamp = sc.taken_at
    embed.set_footer(text=f"id {sc.media_id}")
    if note:
        embed.add_field(name="Media", value=note[:1000], inline=False)
    return embed


def saved_embed(
    base: discord.Embed, tags: dict, labeler_id: str, note: str, image_file: str | None = None
) -> discord.Embed:
    embed = base.copy()
    if image_file:
        # A fetched embed carries the attachment's CDN URL; sent back as-is, Discord shows the
        # image twice (attachment + embed). Pointing at the attachment again keeps it once.
        embed.set_image(url=f"attachment://{image_file}")
    embed.color = SAVED_COLOR
    embed.clear_fields()
    for field_ in base.fields:
        if field_.name == "Media":
            embed.add_field(name=field_.name, value=field_.value, inline=field_.inline)
    embed.add_field(name="Labels", value=describe_tags(tags), inline=False)
    if note:
        embed.add_field(name="Note", value=note[:1000], inline=False)
    embed.add_field(name="Labeled by", value=f"<@{labeler_id}>", inline=False)  # shown, but never pings
    return embed


def label_view(media_id: str, saved: bool = False) -> ui.View:
    view = ui.View(timeout=None)
    view.add_item(LabelButton(media_id, saved))
    return view


class LabelButton(ui.DynamicItem[ui.Button], template=r"swlabel:(?P<media_id>[0-9A-Za-z_-]{1,64})"):
    def __init__(self, media_id: str, saved: bool = False):
        super().__init__(
            ui.Button(
                label="Relabel" if saved else "Label",
                style=discord.ButtonStyle.secondary if saved else discord.ButtonStyle.primary,
                custom_id=f"swlabel:{media_id}",
            )
        )
        self.media_id = media_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match: re.Match):
        return cls(match["media_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        state: LabelState = interaction.client.label_state  # type: ignore[attr-defined]
        if interaction.channel_id != state.cfg.channel_id:
            await interaction.response.send_message("Labels are only taken in the label channel.", ephemeral=True)
            return
        if not state.allowed(interaction.user.id):
            await interaction.response.send_message(REFUSAL, ephemeral=True)
            return
        sidecar = await asyncio.to_thread(find_sidecar, state.cfg.archive_dir, self.media_id)
        existing = await asyncio.to_thread(read_labels, state.cfg.labels_path)
        prior = existing.get(self.media_id)
        if prior:
            selection = {n: list(v) for n, v in prior.tags.items() if v}
            note = prior.note
        else:
            selection = guess_selection(sidecar) if sidecar else {}
            note = ""
        target = sidecar.target if sidecar else (prior.target if prior else "")
        draft = Draft(self.media_id, target, selection, note, interaction.message)
        await interaction.response.send_modal(TagsModal(state, draft))


# -- the modal: one multi-select per dimension ---------------------------------------------------


class TagsModal(ui.Modal):
    def __init__(self, state: LabelState, draft: Draft):
        super().__init__(title="Label this story", timeout=900)
        self.state = state
        self.draft = draft
        self.selects: dict[str, ui.Select] = {}
        for name, cls in DIMENSIONS.items():
            chosen = set(draft.selection.get(name, ()))
            select = ui.Select(
                placeholder="Choose all that apply",
                min_values=0,
                max_values=len(cls),
                required=False,
                options=[discord.SelectOption(label=m.label, value=m.value, default=m.value in chosen) for m in cls],
            )
            self.selects[name] = select
            self.add_item(ui.Label(text=DIMENSION_TITLES[name], component=select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.draft.selection = {name: list(sel.values) for name, sel in self.selects.items()}
        await send_preview(interaction, self.state, self.draft)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.warning("label modal failed (%s)", type(error).__name__)


class NoteModal(ui.Modal):
    def __init__(self, state: LabelState, draft: Draft):
        super().__init__(title="Note (optional)", timeout=900)
        self.state = state
        self.draft = draft
        self.text = ui.TextInput(
            style=discord.TextStyle.paragraph, required=False, max_length=NOTE_MAX, default=draft.note or None
        )
        self.add_item(ui.Label(text="Note", component=self.text))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.draft.note = self.text.value or ""
        await send_preview(interaction, self.state, self.draft)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.warning("note modal failed (%s)", type(error).__name__)


# -- the private preview with Save ---------------------------------------------------------------


def preview_text(draft: Draft) -> str:
    lines = ["**Check these labels, then press Save. Nothing is saved yet.**", describe_selection(draft.selection)]
    dropped = ignored_dimensions(draft.selection)
    if dropped:
        names = ", ".join(DIMENSION_TITLES[n] for n in dropped)
        lines.append(f"_{names}: ignored, it doesn't apply to the chosen post type._")
    try:
        make_label(draft.media_id, draft.target, draft.selection, 0, draft.note)
    except LabelError as e:
        lines.append(f"**Can't save yet:** {e}")
    if draft.note:
        lines.append(f"**Note:** {draft.note}")
    return "\n".join(lines)


async def send_preview(interaction: discord.Interaction, state: LabelState, draft: Draft) -> None:
    await interaction.response.send_message(preview_text(draft), view=PreviewView(state, draft), ephemeral=True)


class PreviewView(ui.View):
    def __init__(self, state: LabelState, draft: Draft):
        super().__init__(timeout=900)  # Discord drops the interaction token after 15 minutes anyway
        self.state = state
        self.draft = draft

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.state.allowed(interaction.user.id):
            await interaction.response.send_message(REFUSAL, ephemeral=True)
            return False
        return True

    @ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if self.is_finished():
            return  # a second click that arrived before the buttons were removed
        d = self.draft
        try:
            label = make_label(d.media_id, d.target, d.selection, interaction.user.id, d.note)
        except LabelError as e:
            await interaction.response.send_message(f"Can't save yet: {e}", ephemeral=True)
            return
        self.stop()
        try:
            await asyncio.to_thread(append_label, self.state.cfg.labels_path, label)
        except OSError as e:
            log.error("could not write labels (%s)", type(e).__name__)
            await interaction.response.send_message("Could not save the label (see the bot log).", ephemeral=True)
            return
        await interaction.response.edit_message(content="Saved.\n" + describe_tags(label.tags), view=None)
        if d.message is not None:
            try:
                base = d.message.embeds[0] if d.message.embeds else discord.Embed()
                # The embed's own upload isn't listed in message.attachments; its name ends the CDN URL.
                image_file = urlsplit(base.image.url).path.rsplit("/", 1)[-1] if base.image.url else None
                await d.message.edit(
                    embed=saved_embed(base, label.tags, label.labeler, label.note, image_file),
                    view=label_view(d.media_id, saved=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                log.warning("could not update the story post (%s)", type(e).__name__)

    @ui.button(label="Add note", style=discord.ButtonStyle.secondary)
    async def add_note(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(NoteModal(self.state, self.draft))

    @ui.button(label="Edit tags", style=discord.ButtonStyle.secondary)
    async def edit_tags(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(TagsModal(self.state, self.draft))


# -- the bot ---------------------------------------------------------------------------------------


class LabelBot(discord.Client):
    def __init__(self, cfg: BotConfig):
        super().__init__(intents=intents(), allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.label_state = LabelState(cfg)
        self.tree = app_commands.CommandTree(self)
        self._poller: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.add_dynamic_items(LabelButton)
        await self._register_pings()
        self._poller = asyncio.create_task(self._poll_forever())

    async def _register_pings(self) -> None:
        """/pings belongs to the label channel's server (one server only). A failure here must
        not stop labeling."""
        try:
            channel = await self.fetch_channel(self.cfg.channel_id)
            await pings_app.register(self, self.tree, self.cfg.db_path, channel.guild.id)  # type: ignore[union-attr]
        except Exception as e:
            log.error("could not register /pings (%s)", type(e).__name__)

    async def on_ready(self) -> None:
        log.info("connected to Discord")

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
        await super().close()

    async def _poll_forever(self) -> None:
        await self.wait_until_ready()
        posted = await asyncio.to_thread(PostedLog, self.cfg.posts_path)
        while not self.is_closed():
            try:
                await self.post_new(posted)
            except Exception as e:  # keep polling; one bad scan must not stop labeling
                log.warning("poll failed (%s)", type(e).__name__)
            await asyncio.sleep(self.cfg.poll_seconds)

    async def post_new(self, posted: PostedLog) -> int:
        """Post stories not yet in the channel, oldest first. Returns how many were posted."""
        sidecars = await asyncio.to_thread(scan_archive, self.cfg.archive_dir)
        to_post, to_skip = select_new(sidecars, posted, self.cfg.since, self.cfg.backlog_max)
        for sc in to_skip:
            await asyncio.to_thread(posted.record, sc.media_id, None, "backlog")
        if to_skip:
            log.info("first start: skipped %d older stories (LABEL_BACKLOG_MAX)", len(to_skip))
        if not to_post:
            await asyncio.to_thread(posted.mark_started)
            return 0
        channel = await self.fetch_channel(self.cfg.channel_id)
        limit = getattr(getattr(channel, "guild", None), "filesize_limit", 10 * 1024 * 1024)
        count = 0
        for sc in to_post[:MAX_POSTS_PER_SCAN]:
            media = choose_media(sc, limit)
            kwargs = {}
            embed = story_embed(sc, media.note)
            if media.path is not None:
                file = discord.File(media.path, filename=media.path.name)
                kwargs["file"] = file
                if media.kind == "image":
                    embed.set_image(url=f"attachment://{file.filename}")
            msg = await channel.send(embed=embed, view=label_view(sc.media_id), **kwargs)
            await asyncio.to_thread(posted.record, sc.media_id, msg.id)
            count += 1
        if count:
            log.info("posted %d new stor%s for labeling", count, "y" if count == 1 else "ies")
        return count
