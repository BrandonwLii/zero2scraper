"""The Discord side of the labeler: posting stories and collecting labels.

Flow. Each story is posted once in the label channel with a "Label" button. The button opens a
modal with one multi-select per tag dimension (a modal holds at most 5 components and there are
5 dimensions). Submitting the modal does not save anything: the labeler gets a private preview
with Save, Add note and Edit tags buttons, and only Save writes the label. The channel message is
then edited to show the saved labels and who saved them.

Restarts. The channel button is a DynamicItem whose custom_id carries the media id, so it keeps
working after a restart without any per-message bookkeeping. The private preview is not
persistent: if the bot restarts mid-label, the labeler just presses Label again.

Fix tags (#35). The message context-menu command "Fix tags" (Apps) works on the watcher's own
webhook posts. It looks the message up in state.db (message id -> story), opens the same tag
modal prefilled with the stored tags, and goes through the same private preview with Save. Save
appends a `source: "correction"` label, replaces the stored tags and edits the post's footer
through the webhook (without pinging anyone). Only LABELER_USER_IDS may use it.

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

from ..notify import DiscordError, Notifier
from ..store import StoryRef
from ..tags import DIMENSIONS
from .config import BotConfig
from .labels import NOTE_MAX, LabelError, append_label, ignored_dimensions, is_allowed, make_label, read_labels
from .queue import PostedLog, Sidecar, choose_media, find_sidecar, scan_archive, select_new
from . import fix, pings_app
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
class FixContext:
    """Set on a draft that corrects a watcher post instead of labeling a label-channel post."""

    ref: StoryRef
    texts: list[str]  # company/title text of the story, to check FAANG+/Quant against the lists


@dataclass
class Draft:
    """An unsaved label. Lives only in memory until Save."""

    media_id: str
    target: str
    selection: dict[str, list[str]]
    note: str = ""
    message: discord.Message | None = None  # the story post to update after saving
    fix: FixContext | None = None


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
        super().__init__(title="Fix tags" if draft.fix else "Label this story", timeout=900)
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
    head = (
        "**Check these tags, then press Save. Nothing is saved yet.** Saving corrects the post's tags "
        "and records a label. Nobody is pinged."
        if draft.fix
        else "**Check these labels, then press Save. Nothing is saved yet.**"
    )
    lines = [head, describe_selection(draft.selection)]
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
    view = FixPreviewView(state, draft) if draft.fix else PreviewView(state, draft)
    await interaction.response.send_message(preview_text(draft), view=view, ephemeral=True)


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


# -- Fix tags: the context-menu command on the watcher's own posts -------------------------------


class FixPreviewView(PreviewView):
    """The preview of a fix. Save corrects the stored tags and the post instead of the label channel."""

    @ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if self.is_finished():
            return
        d = self.draft
        assert d.fix is not None
        try:
            make_label(d.media_id, d.target, d.selection, interaction.user.id, d.note)
        except LabelError as e:
            await interaction.response.send_message(f"Can't save yet: {e}", ephemeral=True)
            return
        self.stop()
        cfg = self.state.cfg
        try:
            result = await asyncio.to_thread(
                fix.apply_fix, cfg.db_path, cfg.labels_path, d.fix.ref, d.selection, interaction.user.id, d.note, d.fix.texts
            )
        except Exception as e:  # LabelError can't happen here; OSError, sqlite3.Error
            log.error("could not save the fix (%s)", type(e).__name__)
            await interaction.response.send_message("Could not save the fix (see the bot log).", ephemeral=True)
            return
        text = "Saved. The tags are corrected and the correction is recorded as a label.\n" + describe_tags(result.label.tags)
        if result.notes:
            text += "\n\n" + "\n".join(f"**Note:** {n}" for n in result.notes)
        await interaction.response.edit_message(content=text + "\n\n_Updating the post…_", view=None)
        outcome = await self._update_post(d, result.footer)
        await interaction.edit_original_response(content=f"{text}\n\n{outcome}")

    async def _update_post(self, d: Draft, footer: str) -> str:
        cfg = self.state.cfg
        if not cfg.webhook_url:
            return "_The post itself was not edited (DISCORD_WEBHOOK is not set for the bot)._"
        if d.message is None or not d.message.embeds:
            return "_The post itself was not edited (no embed on it)._"
        embed = fix.embed_with_footer(d.message.embeds[0].to_dict(), footer)
        try:
            await asyncio.to_thread(Notifier(cfg.webhook_url).edit_message, str(d.message.id), {"embeds": [embed]}, 2)
        except DiscordError as e:  # messages never contain the webhook URL
            log.warning("could not edit the story post (%s)", e)
            return "_The post itself could not be edited (see the bot log)._"
        except Exception as e:  # anything else: type only, so no traceback can carry the URL
            log.warning("could not edit the story post (%s)", type(e).__name__)
            return "_The post itself could not be edited (see the bot log)._"
        return "_The post now shows the corrected tags._"


def fix_tags_command(state: LabelState) -> app_commands.ContextMenu:
    async def fix_tags(interaction: discord.Interaction, message: discord.Message) -> None:
        if not state.allowed(interaction.user.id):
            await interaction.response.send_message(REFUSAL, ephemeral=True)
            return
        cfg = state.cfg
        try:
            found = await asyncio.to_thread(fix.lookup, cfg.db_path, str(message.id))
        except Exception as e:
            log.error("could not look up the story (%s)", type(e).__name__)
            await interaction.response.send_message("Could not read the database (see the bot log).", ephemeral=True)
            return
        if found is None:
            await interaction.response.send_message(
                "That isn't a story post I know. Fix tags works on the watcher's posts from the last two days.",
                ephemeral=True,
            )
            return
        ref, tags = found
        sidecar = await asyncio.to_thread(find_sidecar, cfg.archive_dir, ref.media_id)
        texts = [t for t in (sidecar.company, sidecar.job_title) if t] if sidecar else []
        if message.embeds and message.embeds[0].title:
            texts.append(message.embeds[0].title)
        draft = Draft(ref.media_id, ref.target, fix.selection_from_tags(tags), "", message, FixContext(ref, texts))
        await interaction.response.send_modal(TagsModal(state, draft))

    return app_commands.ContextMenu(name="Fix tags", callback=fix_tags, guild_ids=None)


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
        await self._register_commands()
        self._poller = asyncio.create_task(self._poll_forever())

    async def _register_commands(self) -> None:
        """/pings and Fix tags belong to the label channel's server (one server only). A failure
        here must not stop labeling. One sync (inside pings_app.register) publishes both."""
        try:
            channel = await self.fetch_channel(self.cfg.channel_id)
            guild_id = channel.guild.id  # type: ignore[union-attr]
        except Exception as e:
            log.error("could not register commands (%s)", type(e).__name__)
            return
        self.tree.add_command(fix_tags_command(self.label_state), guild=discord.Object(id=guild_id))
        try:
            await pings_app.register(self, self.tree, self.cfg.db_path, guild_id)
        except Exception as e:
            log.error("could not register /pings and Fix tags (%s)", type(e).__name__)

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
