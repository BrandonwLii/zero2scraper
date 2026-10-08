"""Smoke tests for the discord glue, with fakes. No network; nothing connects to Discord."""

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
from test_bot_logic import CHANNEL, OTHER, T0, USER, env, write_sidecar

from story_watch.bot import discord_app as app
from story_watch.bot.config import load_bot_config
from story_watch.bot.labels import read_labels
from story_watch.bot.queue import PostedLog
from story_watch.tags import DIMENSIONS


def run(coro):
    return asyncio.run(coro)


class Resp:
    def __init__(self):
        self.calls = []

    async def send_message(self, content=None, **kw):
        self.calls.append(("send", content, kw))

    async def edit_message(self, **kw):
        self.calls.append(("edit", None, kw))

    async def send_modal(self, modal):
        self.calls.append(("modal", None, {"modal": modal}))


def interaction(client, user_id, message=None, channel_id=int(CHANNEL)):
    return SimpleNamespace(
        client=client, user=SimpleNamespace(id=int(user_id)), channel_id=channel_id, message=message, response=Resp()
    )


class FakeMessage:
    def __init__(self, embed=None):
        self.id = 777
        self.embeds = [embed or discord.Embed(description="**Account:** alice")]
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeChannel:
    def __init__(self, limit=1_000_000):
        self.guild = SimpleNamespace(filesize_limit=limit)
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=900 + len(self.sent))


def make_bot(tmp_path):
    return app.LabelBot(load_bot_config(env(tmp_path)))


def test_intents_are_not_privileged():
    i = app.intents()
    assert not (i.members or i.presences or i.message_content)


def test_posts_oldest_first_with_image_and_records_it(tmp_path):
    for n, mid in enumerate(["a", "b"]):
        folder = write_sidecar(tmp_path, "alice", mid, T0.replace(minute=n), files=[f"{mid}.jpg"], category="job_posting")
        (folder / f"{mid}.jpg").write_bytes(b"x")
    ch = FakeChannel()

    async def go():
        bot = make_bot(tmp_path)

        async def fetch(_id):
            return ch

        bot.fetch_channel = fetch
        posted = PostedLog(bot.cfg.posts_path)
        assert await bot.post_new(posted) == 2
        assert await bot.post_new(posted) == 0
        return PostedLog(bot.cfg.posts_path)

    posted = run(go())
    assert [m["embed"].footer.text for m in ch.sent] == ["id a", "id b"]
    assert ch.sent[0]["file"].filename == "a.jpg"
    assert ch.sent[0]["embed"].image.url == "attachment://a.jpg"
    assert posted.message_id("a") == 901 and posted.message_id("b") == 902
    assert "Job posting" in ch.sent[0]["embed"].description


def test_restart_does_not_repost(tmp_path):
    write_sidecar(tmp_path, "alice", "a", T0)
    ch = FakeChannel()

    async def go():
        for _ in range(2):  # two "process lifetimes"
            bot = make_bot(tmp_path)

            async def fetch(_id):
                return ch

            bot.fetch_channel = fetch
            await bot.post_new(PostedLog(bot.cfg.posts_path))

    run(go())
    assert len(ch.sent) == 1


def test_big_video_is_not_uploaded(tmp_path):
    folder = write_sidecar(tmp_path, "alice", "v", T0, files=["v.mp4"], is_video=True)
    (folder / "v.mp4").write_bytes(b"0" * 3000)
    ch = FakeChannel(limit=1000)

    async def go():
        bot = make_bot(tmp_path)

        async def fetch(_id):
            return ch

        bot.fetch_channel = fetch
        await bot.post_new(PostedLog(bot.cfg.posts_path))

    run(go())
    [msg] = ch.sent
    assert "file" not in msg
    assert "v.mp4" in msg["embed"].fields[0].value


def test_story_button_survives_restart_via_custom_id(tmp_path):
    async def go():
        view = app.label_view("12345")
        [item] = view.children
        assert item.custom_id == "swlabel:12345"
        rebuilt = await app.LabelButton.from_custom_id(None, item.item, app.LabelButton.__discord_ui_compiled_template__.fullmatch("swlabel:12345"))
        return rebuilt.media_id

    assert run(go()) == "12345"


def test_button_refuses_strangers_and_wrong_channel(tmp_path):
    async def go():
        bot = make_bot(tmp_path)
        button = app.LabelButton("m1")
        stranger = interaction(bot, 111111111111111111)
        await button.callback(stranger)
        elsewhere = interaction(bot, USER, channel_id=5)
        await button.callback(elsewhere)
        return stranger, elsewhere

    stranger, elsewhere = run(go())
    for i in (stranger, elsewhere):
        [(kind, _, kw)] = i.response.calls
        assert kind == "send" and kw["ephemeral"] is True


def test_button_opens_modal_prefilled_with_guess(tmp_path):
    write_sidecar(tmp_path, "alice", "m1", T0, category="job_posting")

    async def go():
        bot = make_bot(tmp_path)
        i = interaction(bot, USER, FakeMessage())
        await app.LabelButton("m1").callback(i)
        return i.response.calls[0][2]["modal"]

    modal = run(go())
    assert list(modal.selects) == list(DIMENSIONS)
    chosen = [o.value for o in modal.selects["post_type"].options if o.default]
    assert chosen == ["job_posting"]
    assert not any(o.default for o in modal.selects["role"].options)
    assert modal.draft.target == "alice"
    assert len(modal.children) <= 5  # Discord's cap on modal components


def test_modal_options_fit_discord_limits(tmp_path):
    async def go():
        bot = make_bot(tmp_path)
        draft = app.Draft("m", "t", {})
        return app.TagsModal(app.LabelState(bot.cfg), draft)

    modal = run(go())
    for name, select in modal.selects.items():
        assert len(select.options) <= 25 and select.max_values == len(select.options)
        assert select.min_values == 0 and not select.required
        assert all(len(o.label) <= 100 and len(o.value) <= 100 for o in select.options)


def full_draft(message=None):
    return app.Draft(
        "m1", "alice",
        {"post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["quant"], "role": ["swe", "pm"],
         "level": ["internship"]},
        "a note", message,
    )


def test_nothing_is_saved_until_save_is_pressed(tmp_path):
    async def go():
        bot = make_bot(tmp_path)
        i = interaction(bot, USER)
        await app.send_preview(i, app.LabelState(bot.cfg), full_draft())
        return bot, i

    bot, i = run(go())
    assert i.response.calls[0][2]["ephemeral"] is True
    assert "Nothing is saved yet" in i.response.calls[0][1]
    assert not bot.cfg.labels_path.exists()


def test_save_writes_label_and_updates_the_post(tmp_path):
    async def go():
        bot = make_bot(tmp_path)
        msg = FakeMessage()
        view = app.PreviewView(app.LabelState(bot.cfg), full_draft(msg))
        i = interaction(bot, OTHER)
        assert await view.interaction_check(i)
        await view.save.callback(i)
        return bot, msg, i

    bot, msg, i = run(go())
    got = read_labels(bot.cfg.labels_path)["m1"]
    assert got.labeler == OTHER and got.tags["role"] == ["swe", "pm"] and got.note == "a note"
    [edit] = msg.edits
    names = [f.name for f in edit["embed"].fields]
    assert names == ["Labels", "Note", "Labeled by"]
    assert f"<@{OTHER}>" in edit["embed"].fields[-1].value
    assert edit["allowed_mentions"].users is False
    assert i.response.calls[0][0] == "edit"


def test_save_with_missing_dimension_is_refused(tmp_path):
    async def go():
        bot = make_bot(tmp_path)
        draft = full_draft()
        draft.selection["role"] = []
        view = app.PreviewView(app.LabelState(bot.cfg), draft)
        i = interaction(bot, USER)
        await view.save.callback(i)
        return bot, i

    bot, i = run(go())
    assert not bot.cfg.labels_path.exists()
    assert "role" in i.response.calls[0][1]


def test_preview_view_refuses_non_labelers(tmp_path):
    async def go():
        bot = make_bot(tmp_path)
        view = app.PreviewView(app.LabelState(bot.cfg), full_draft())
        i = interaction(bot, 111111111111111111)
        return await view.interaction_check(i), i

    ok, i = run(go())
    assert not ok and i.response.calls[0][2]["ephemeral"] is True


def test_relabel_prefills_last_label_and_last_wins(tmp_path):
    write_sidecar(tmp_path, "alice", "m1", T0, category="misc")

    async def go():
        bot = make_bot(tmp_path)
        state = app.LabelState(bot.cfg)
        await app.PreviewView(state, full_draft(FakeMessage())).save.callback(interaction(bot, USER))
        i = interaction(bot, USER, FakeMessage())
        await app.LabelButton("m1").callback(i)
        modal = i.response.calls[0][2]["modal"]
        modal.draft.selection = {"post_type": ["misc"]}
        await app.PreviewView(state, modal.draft).save.callback(interaction(bot, OTHER))
        return bot, modal

    bot, modal = run(go())
    assert [o.value for o in modal.selects["role"].options if o.default] == ["swe", "pm"]
    assert modal.draft.note == "a note"
    lines = bot.cfg.labels_path.read_text().splitlines()
    assert len(lines) == 2
    final = read_labels(bot.cfg.labels_path)["m1"]
    assert final.tags["role"] is None and final.labeler == OTHER and json.loads(lines[0])["labeler"] == USER


def test_log_never_contains_token_on_login_failure(tmp_path, monkeypatch, caplog):
    from story_watch.bot import main as bot_main

    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)

    class Boom:
        def __init__(self, cfg):
            pass

        def run(self, token, **kw):
            raise discord.LoginFailure(f"Improper token has been passed: {token}")

    monkeypatch.setattr(app, "LabelBot", Boom)
    assert bot_main.cli([]) == 2
    assert "fake-token-value" not in caplog.text
