"""Fix tags (#35): the pure parts and the discord glue, with fakes. No network."""

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
from conftest import make_item
from test_bot_discord import FakeMessage, Resp, interaction, make_bot, run
from test_bot_logic import OTHER, T0, USER, env, write_sidecar

from story_watch.bot import discord_app as app
from story_watch.bot import fix
from story_watch.bot.config import BotConfigError, load_bot_config
from story_watch.bot.labels import LabelError, read_labels
from story_watch.notify import DiscordError
from story_watch.store import Store
from story_watch.tags import Company, Role, Tags

MSG = "555555555555555555"
STRANGER = "999999999999999999"
HOOK = "https://discord.com/api/webhooks/1/secret"


def fixture_db(tmp_path, tags=None):
    item = make_item("m1")
    store = Store(tmp_path / "state.db")
    store.save_message(MSG, item)
    if tags is not None:
        store.save_tags(item, tags)
    store.close()
    return item


def fix_bot(tmp_path, **extra):
    return app.LabelBot(load_bot_config(env(tmp_path, DB_PATH=str(tmp_path / "state.db"), **extra)))


JOB = {"post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["other"], "role": ["swe"], "level": ["internship"]}


# -- pure ----------------------------------------------------------------------------------------


def test_unsure_dimensions_start_empty_in_the_picker():
    tags = Tags(post_type=["job_posting"], role=[Role.SWE, Role.PM], company=[Company.QUANT])
    sel = fix.selection_from_tags(tags)
    assert sel["post_type"] == ["job_posting"] and sel["role"] == ["swe", "pm"] and sel["company"] == ["quant"]
    assert "sponsorship" not in sel and "level" not in sel  # the tagger had no idea
    assert fix.selection_from_tags(None) == {}


def test_embed_is_rebuilt_with_only_known_fields_and_the_new_footer():
    fetched = {
        "type": "rich", "title": "t", "url": "https://x.example/", "color": 1, "timestamp": "2026-01-01T00:00:00+00:00",
        "fields": [{"name": "a", "value": "b", "inline": True}],
        "image": {"url": "https://cdn.example/i.jpg", "proxy_url": "https://p", "width": 1, "height": 2},
        "author": {"name": "New story from @x", "icon_url": "https://i"},
        "footer": {"text": "old", "proxy_icon_url": "https://q"},
    }
    out = fix.embed_with_footer(fetched, "new")
    assert out["footer"] == {"text": "new"} and out["image"] == {"url": "https://cdn.example/i.jpg"}
    assert out["author"] == {"name": "New story from @x"} and "type" not in out and out["fields"] == fetched["fields"]


def test_company_feedback_only_when_the_company_is_off_the_list():
    assert fix.company_feedback({"company": ["faang_plus"]}, ["Totally Unlisted Corp: SWE Intern"])
    assert fix.company_feedback({"company": ["quant"]}, ["Totally Unlisted Corp"])[0].startswith("Quant was set")
    assert not fix.company_feedback({"company": ["faang_plus"]}, ["Stripe: SWE Intern"])
    assert not fix.company_feedback({"company": ["other"]}, ["Totally Unlisted Corp"])
    assert not fix.company_feedback({"company": ["faang_plus"]}, [])  # nothing to check against


def test_apply_fix_writes_label_tags_and_footer(tmp_path, caplog):
    fixture_db(tmp_path, Tags.unsure())
    ref = Store(tmp_path / "state.db").story_for_message(MSG)
    labels = tmp_path / "labels.jsonl"
    caplog.set_level("INFO")
    res = fix.apply_fix(tmp_path / "state.db", labels, ref, {**JOB, "company": ["faang_plus"]}, USER, "n", ["Unlisted Co"])
    line = json.loads(labels.read_text())
    assert line["source"] == "correction" and line["labeler"] == USER and line["media_id"] == "m1"
    assert line["company"] == ["faang_plus"]
    store = Store(tmp_path / "state.db")
    stored = store.get_tags("m1")
    assert stored.certain_value("role") == Role.SWE and stored.confidence == 1.0
    assert res.footer.endswith("· corrected") and "SWE" in res.footer
    assert len(res.notes) == 1 and "Unlisted Co" in caplog.text


def test_apply_fix_with_a_bad_selection_writes_nothing(tmp_path):
    fixture_db(tmp_path)
    ref = Store(tmp_path / "state.db").story_for_message(MSG)
    with pytest.raises(LabelError):
        fix.apply_fix(tmp_path / "state.db", tmp_path / "labels.jsonl", ref, {"post_type": ["job_posting"]}, USER)
    assert not (tmp_path / "labels.jsonl").exists() and Store(tmp_path / "state.db").get_tags("m1") is None


def test_label_channel_labels_have_no_source_key_and_old_lines_still_read(tmp_path):
    from story_watch.bot.labels import append_label, make_label

    path = tmp_path / "l.jsonl"
    append_label(path, make_label("a", "t", JOB, USER))
    append_label(path, make_label("b", "t", JOB, USER, source="correction"))
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert "source" not in lines[0] and lines[1]["source"] == "correction"
    got = read_labels(path)
    assert got["a"].source == "" and got["b"].source == "correction"


def test_bot_config_takes_the_webhook_but_never_shows_it(tmp_path):
    cfg = load_bot_config(env(tmp_path, DISCORD_WEBHOOK=HOOK))
    assert cfg.webhook_url == HOOK and HOOK not in repr(cfg) and "edit_posts=True" in repr(cfg)
    assert load_bot_config(env(tmp_path)).webhook_url == ""
    with pytest.raises(BotConfigError) as e:
        load_bot_config(env(tmp_path, DISCORD_WEBHOOK="http://evil.example/x-secret"))
    assert "secret" not in str(e.value)


# -- discord glue --------------------------------------------------------------------------------


class RecordingInteraction:
    def __init__(self, client, user_id, message):
        self.__dict__.update(vars(interaction(client, user_id, message=message)))
        self.original = []

    async def edit_original_response(self, **kw):
        self.original.append(kw)


def command(bot):
    return app.fix_tags_command(bot.label_state)


def test_command_refuses_strangers(tmp_path):
    bot = fix_bot(tmp_path)
    fixture_db(tmp_path)
    i = interaction(bot, STRANGER)
    run(command(bot).callback(i, FakeMessage()))
    assert i.response.calls[0][1] == app.REFUSAL and i.response.calls[0][2]["ephemeral"]


def test_command_on_an_unknown_message_says_so(tmp_path):
    bot = fix_bot(tmp_path)
    msg = FakeMessage()
    msg.id = 1
    i = interaction(bot, USER)
    run(command(bot).callback(i, msg))
    kind, text, kw = i.response.calls[0]
    assert kind == "send" and "isn't a story post" in text and kw["ephemeral"]


def test_command_opens_the_modal_prefilled_with_the_stored_tags(tmp_path):
    bot = fix_bot(tmp_path)
    fixture_db(tmp_path, Tags(post_type=["job_posting"], role=[Role.PM], company=[Company.QUANT]))
    msg = FakeMessage()
    msg.id = int(MSG)
    i = interaction(bot, USER, message=msg)
    run(command(bot).callback(i, msg))
    kind, _, kw = i.response.calls[0]
    modal = kw["modal"]
    assert kind == "modal" and modal.title == "Fix tags"
    chosen = {n: [o.value for o in s.options if o.default] for n, s in modal.selects.items()}
    assert chosen["role"] == ["pm"] and chosen["company"] == ["quant"] and chosen["post_type"] == ["job_posting"]
    assert chosen["level"] == []


def test_preview_of_a_fix_saves_nothing_and_says_nobody_is_pinged(tmp_path):
    bot = fix_bot(tmp_path)
    ref = Store(fixture_db(tmp_path) and tmp_path / "state.db").story_for_message(MSG)
    draft = app.Draft("m1", "alice", JOB, "", FakeMessage(), app.FixContext(ref, []))
    i = interaction(bot, USER)
    run(app.send_preview(i, app.LabelState(bot.cfg), draft))
    assert "Nobody is pinged" in i.response.calls[0][1] and isinstance(i.response.calls[0][2]["view"], app.FixPreviewView)
    assert not bot.cfg.labels_path.exists()


class FakeNotifier:
    edits: list = []
    error: Exception | None = None

    def __init__(self, url):
        self.url = url

    def edit_message(self, message_id, payload, max_attempts=None):
        if FakeNotifier.error:
            raise FakeNotifier.error
        FakeNotifier.edits.append((self.url, message_id, payload))


def save_fix(bot, monkeypatch, user=USER, **ctx):
    FakeNotifier.edits, FakeNotifier.error = [], ctx.pop("error", None)
    monkeypatch.setattr(app, "Notifier", FakeNotifier)
    fixture_db(bot.cfg.db_path.parent)
    ref = Store(bot.cfg.db_path).story_for_message(MSG)
    embed = discord.Embed(title="Stripe: SWE Intern", description=None)
    embed.set_footer(text="Job posting · old")
    msg = FakeMessage(embed)
    msg.id = int(MSG)
    view = app.FixPreviewView(app.LabelState(bot.cfg), app.Draft("m1", "alice", JOB, "", msg, app.FixContext(ref, ["Stripe"])))
    i = RecordingInteraction(bot, user, msg)
    run(view.save.callback(i))
    return i, msg


def test_save_a_fix_end_to_end(tmp_path, monkeypatch):
    bot = fix_bot(tmp_path, DISCORD_WEBHOOK=HOOK)
    i, msg = save_fix(bot, monkeypatch)
    label = read_labels(bot.cfg.labels_path)["m1"]
    assert label.source == "correction" and label.labeler == USER
    assert Store(bot.cfg.db_path).get_tags("m1").certain_value("role") == Role.SWE
    [(url, mid, payload)] = FakeNotifier.edits
    assert url == HOOK and mid == MSG and set(payload) == {"embeds"}  # content untouched: no new pings
    assert payload["embeds"][0]["footer"]["text"].endswith("· corrected")
    assert payload["embeds"][0]["title"] == "Stripe: SWE Intern"
    assert i.response.calls[0][0] == "edit" and "corrected tags" in i.original[0]["content"]
    assert msg.edits == []  # the bot never edits through its own token (it isn't the author)


def test_save_a_fix_without_a_webhook_still_records_it(tmp_path, monkeypatch):
    bot = fix_bot(tmp_path)
    i, _ = save_fix(bot, monkeypatch)
    assert read_labels(bot.cfg.labels_path)["m1"].source == "correction"
    assert FakeNotifier.edits == [] and "was not edited" in i.original[0]["content"]


def test_a_failed_post_edit_is_reported_but_the_fix_stands(tmp_path, monkeypatch, caplog):
    bot = fix_bot(tmp_path, DISCORD_WEBHOOK=HOOK)
    i, _ = save_fix(bot, monkeypatch, error=DiscordError("discord rejected message: HTTP 404"))
    assert "could not be edited" in i.original[0]["content"]
    assert read_labels(bot.cfg.labels_path)["m1"].source == "correction"
    assert HOOK not in caplog.text


def test_an_unexpected_edit_error_is_logged_by_type_only(tmp_path, monkeypatch, caplog):
    bot = fix_bot(tmp_path, DISCORD_WEBHOOK=HOOK)
    i, _ = save_fix(bot, monkeypatch, error=RuntimeError(f"boom {HOOK}"))
    assert "could not be edited" in i.original[0]["content"]
    assert read_labels(bot.cfg.labels_path)["m1"].source == "correction"
    assert "RuntimeError" in caplog.text and HOOK not in caplog.text


def test_fix_preview_refuses_non_labelers(tmp_path):
    bot = fix_bot(tmp_path)
    view = app.FixPreviewView(app.LabelState(bot.cfg), app.Draft("m1", "a", JOB))
    i = interaction(bot, STRANGER)
    assert not run(view.interaction_check(i))


def test_company_note_reaches_the_confirmation(tmp_path, monkeypatch):
    bot = fix_bot(tmp_path, DISCORD_WEBHOOK=HOOK)
    FakeNotifier.edits, FakeNotifier.error = [], None
    monkeypatch.setattr(app, "Notifier", FakeNotifier)
    fixture_db(tmp_path)
    ref = Store(bot.cfg.db_path).story_for_message(MSG)
    sel = {**JOB, "company": ["quant"]}
    view = app.FixPreviewView(app.LabelState(bot.cfg), app.Draft("m1", "alice", sel, "", None, app.FixContext(ref, ["Unlisted Co"])))
    i = RecordingInteraction(bot, USER, None)
    run(view.save.callback(i))
    assert "Quant was set" in i.response.calls[0][2]["content"]
    assert "no embed" in i.original[0]["content"]
