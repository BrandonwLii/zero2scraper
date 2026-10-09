"""Pure logic of the label bot: config, validation, labels file, archive scan, posted log."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_item

from story_watch.archive import Archive
from story_watch.bot.config import BotConfigError, load_bot_config, parse_user_ids
from story_watch.bot.labels import (
    TAXONOMY_VERSION,
    LabelError,
    append_label,
    ignored_dimensions,
    is_allowed,
    make_label,
    read_labels,
    validate,
)
from story_watch.bot.main import RedactToken, cli
from story_watch.bot.queue import PostedLog, Sidecar, choose_media, find_sidecar, scan_archive, select_new
from story_watch.bot.render import describe_tags, guess_selection, story_description
from story_watch.classify import Category
from story_watch.tags import DIMENSIONS

CHANNEL = "100000000000000001"
USER = "100000000000000002"
OTHER = "100000000000000003"
T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def env(tmp_path, **extra):
    base = {
        "DISCORD_BOT_TOKEN": "fake-token-value",
        "LABEL_CHANNEL_ID": CHANNEL,
        "LABELER_USER_IDS": f"{USER}, {OTHER}",
        "ARCHIVE_DIR": str(tmp_path),
    }
    base.update(extra)
    return base


# -- config -----------------------------------------------------------------------------------


def test_config_parses_and_defaults(tmp_path):
    cfg = load_bot_config(env(tmp_path))
    assert cfg.channel_id == int(CHANNEL)
    assert cfg.labeler_ids == {int(USER), int(OTHER)}
    assert cfg.labels_path == tmp_path / "labels.jsonl"
    assert cfg.posts_path.parent == tmp_path  # top level, away from prune()'s */* glob
    assert (cfg.poll_seconds, cfg.backlog_max, cfg.since) == (30, 10, None)


def test_config_never_shows_token(tmp_path):
    cfg = load_bot_config(env(tmp_path))
    assert "fake-token-value" not in repr(cfg) + str(cfg)


@pytest.mark.parametrize(
    "drop",
    ["DISCORD_BOT_TOKEN", "LABEL_CHANNEL_ID", "LABELER_USER_IDS", "ARCHIVE_DIR"],
)
def test_config_requires_core_keys(tmp_path, drop):
    e = env(tmp_path)
    del e[drop]
    with pytest.raises(BotConfigError) as err:
        load_bot_config(e)
    assert "fake-token-value" not in str(err.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"LABEL_CHANNEL_ID": "abc"},
        {"LABELER_USER_IDS": "123"},
        {"ARCHIVE_DIR": "relative/dir"},
        {"LABEL_POLL_SECONDS": "1"},
        {"LABEL_BACKLOG_MAX": "-1"},
        {"LABEL_SINCE": "yesterday"},
    ],
)
def test_config_rejects_bad_values(tmp_path, extra):
    with pytest.raises(BotConfigError):
        load_bot_config(env(tmp_path, **extra))


def test_config_since_and_overrides(tmp_path):
    cfg = load_bot_config(env(tmp_path, LABEL_SINCE="2026-02-03", LABEL_POLL_SECONDS="60", LABEL_BACKLOG_MAX="0"))
    assert cfg.since == datetime(2026, 2, 3, tzinfo=timezone.utc)
    assert (cfg.poll_seconds, cfg.backlog_max) == (60, 0)


def test_labels_file_may_not_hide_in_a_target_folder(tmp_path):
    with pytest.raises(BotConfigError):
        load_bot_config(env(tmp_path, LABELS_FILE=str(tmp_path / "alice" / "labels.jsonl")))
    ok = load_bot_config(env(tmp_path, LABELS_FILE=str(tmp_path / "mine.jsonl")))
    assert ok.labels_path == tmp_path / "mine.jsonl"


def test_user_ids_accept_mention_form():
    assert parse_user_ids(f"<@{USER}>,{OTHER}") == {int(USER), int(OTHER)}


def test_allowlist():
    assert is_allowed(1, frozenset({1, 2}))
    assert not is_allowed(3, frozenset({1, 2}))
    assert not is_allowed(3, frozenset())


def test_cli_config_error_exits_2_without_token_in_output(tmp_path, monkeypatch, caplog):
    for k, v in env(tmp_path, LABELER_USER_IDS="").items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    assert cli([]) == 2
    assert "fake-token-value" not in caplog.text


def test_redact_filter_masks_token():
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "login with %s", ("fake-token-value",), None)
    assert RedactToken("fake-token-value").filter(rec)
    assert "fake-token-value" not in rec.getMessage()


# -- validation ---------------------------------------------------------------------------------


def test_job_posting_needs_every_dimension():
    with pytest.raises(LabelError) as e:
        validate({"post_type": ["job_posting"], "role": ["swe"]})
    assert "sponsorship" in str(e.value) and "company" in str(e.value) and "level" in str(e.value)


def test_full_job_posting_label_multi_values_in_enum_order():
    out = validate(
        {
            "post_type": ["job_posting"],
            "sponsorship": ["unknown"],
            "company": ["other", "quant"],
            "role": ["pm", "swe"],
            "level": ["internship"],
        }
    )
    assert out["role"] == ["swe", "pm"] and out["company"] == ["quant", "other"]


def test_misc_makes_everything_else_not_applicable():
    out = validate({"post_type": ["misc"], "role": ["swe"], "sponsorship": ["unknown"]})
    assert out == {"post_type": ["misc"], "sponsorship": None, "company": None, "role": None, "level": None}
    assert ignored_dimensions({"post_type": ["misc"], "role": ["swe"]}) == ["role"]


def test_event_skips_only_sponsorship():
    out = validate({"post_type": ["event"], "company": ["quant"], "role": ["ml"], "level": ["new_grad"]})
    assert out["sponsorship"] is None and out["company"] == ["quant"]


def test_dimension_applies_if_any_post_type_needs_it():
    out = validate(
        {"post_type": ["misc", "job_posting"], "sponsorship": ["unknown"], "company": ["other"],
         "role": ["swe"], "level": ["other"]}
    )
    assert out["sponsorship"] == ["unknown"]


@pytest.mark.parametrize(
    "sel",
    [{}, {"post_type": []}, {"post_type": ["nope"]}, {"post_type": ["misc"], "bogus": ["x"]},
     {"post_type": ["job_posting"], "role": ["president"]}],
)
def test_invalid_selections(sel):
    with pytest.raises(LabelError):
        validate(sel)


def test_ignored_dimensions_empty_without_post_type():
    assert ignored_dimensions({"role": ["swe"]}) == ["role"]


# -- labels file --------------------------------------------------------------------------------


def label(media_id="m1", labeler=USER, note="", role=("swe",), now=T0):
    return make_label(
        media_id, "alice",
        {"post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["other"],
         "role": list(role), "level": ["internship"]},
        labeler, note, now,
    )


def test_label_line_format(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, label(note="  hi  "))
    doc = json.loads(path.read_text())
    assert list(doc) == ["media_id", "target", "post_type", "sponsorship", "company", "role", "level",
                         "labeler", "note", "labeled_at", "taxonomy"]
    assert doc["labeler"] == USER and doc["note"] == "hi"
    assert doc["labeled_at"] == "2026-01-01T12:00:00+00:00"
    assert doc["taxonomy"] == TAXONOMY_VERSION and TAXONOMY_VERSION.startswith("tags-")
    assert set(DIMENSIONS) <= set(doc)


def test_not_applicable_is_null(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, make_label("m2", "alice", {"post_type": ["misc"]}, USER))
    doc = json.loads(path.read_text())
    assert doc["post_type"] == ["misc"] and doc["role"] is None and doc["sponsorship"] is None


def test_last_label_wins_and_survives_garbage(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, label("m1", role=("swe",)))
    append_label(path, label("m2"))
    with open(path, "a") as fh:
        fh.write("not json\n\n[1]\n")
    append_label(path, label("m1", labeler=OTHER, role=("pm",), note="changed"))
    got = read_labels(path)
    assert set(got) == {"m1", "m2"}
    assert got["m1"].tags["role"] == ["pm"] and got["m1"].labeler == OTHER and got["m1"].note == "changed"
    assert len(path.read_text().splitlines()) == 6  # history is kept


def test_read_missing_file(tmp_path):
    assert read_labels(tmp_path / "none.jsonl") == {}


def test_note_limit():
    with pytest.raises(LabelError):
        label(note="x" * 501)


def test_labels_file_is_private(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, label())
    assert path.stat().st_mode & 0o077 == 0


# -- archive scan, posted log, selection -----------------------------------------------------------


def write_sidecar(root, target, media_id, taken_at, files=(), category="misc", **extra):
    folder = root / target
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{taken_at.strftime('%Y%m%dT%H%M%SZ')}_{media_id}"
    doc = {
        "media_id": media_id, "target": target, "taken_at": taken_at.isoformat(), "is_video": False,
        "links": [], "mentions": [], "job_title": None, "company": None, "classifier": "rules",
        "category": category, "files": list(files), "download_error": None, "node": {}, "page_node": None,
    }
    doc.update(extra)
    (folder / f"{stem}.json").write_text(json.dumps(doc))
    return folder


def test_scan_orders_oldest_first_and_skips_junk(tmp_path):
    write_sidecar(tmp_path, "alice", "b", T0 + timedelta(minutes=2))
    write_sidecar(tmp_path, "bob", "a", T0)
    write_sidecar(tmp_path, "alice", "c", T0 + timedelta(minutes=1))
    (tmp_path / "alice" / "broken.json").write_text("{")
    (tmp_path / "alice" / "x.json.part").write_text("{}")
    (tmp_path / "labels.jsonl").write_text("")  # top-level files are not targets
    assert [s.media_id for s in scan_archive(tmp_path)] == ["a", "c", "b"]
    assert scan_archive(tmp_path / "missing") == []
    assert find_sidecar(tmp_path, "c").target == "alice" and find_sidecar(tmp_path, "zzz") is None


def test_reads_what_archive_actually_writes(tmp_path):
    """Guards the sidecar format: build it with the watcher's own code."""
    item = make_item("12345", "alice")
    doc = Archive._sidecar(item, Category.JOB_POSTING, "rules", ["x.jpg"], None)
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "s.json").write_text(json.dumps(doc, default=str))
    [sc] = scan_archive(tmp_path)
    assert (sc.media_id, sc.target, sc.category, sc.files) == ("12345", "alice", "job_posting", ("x.jpg",))
    assert sc.taken_at.tzinfo is not None


def sidecars(tmp_path, n):
    for i in range(n):
        write_sidecar(tmp_path, "alice", f"m{i}", T0 + timedelta(minutes=i))
    return scan_archive(tmp_path)


def test_first_start_posts_newest_and_skips_the_rest(tmp_path):
    posted = PostedLog(tmp_path / "posts.jsonl")
    assert posted.first_run
    to_post, to_skip = select_new(sidecars(tmp_path, 5), posted, backlog_max=2)
    assert [s.media_id for s in to_post] == ["m3", "m4"]
    assert [s.media_id for s in to_skip] == ["m0", "m1", "m2"]


def test_later_starts_post_everything_new(tmp_path):
    posted = PostedLog(tmp_path / "posts.jsonl")
    posted.record("m0", 1)
    again = PostedLog(tmp_path / "posts.jsonl")
    assert not again.first_run and "m0" in again and again.message_id("m0") == 1
    to_post, to_skip = select_new(sidecars(tmp_path, 5), again, backlog_max=1)
    assert [s.media_id for s in to_post] == ["m1", "m2", "m3", "m4"] and to_skip == []


def test_skipped_stay_skipped(tmp_path):
    posted = PostedLog(tmp_path / "posts.jsonl")
    posted.record("m0", None, "backlog")
    to_post, _ = select_new(sidecars(tmp_path, 2), PostedLog(tmp_path / "posts.jsonl"))
    assert [s.media_id for s in to_post] == ["m1"]


def test_since_filters_old(tmp_path):
    posted = PostedLog(tmp_path / "posts.jsonl")
    to_post, to_skip = select_new(sidecars(tmp_path, 4), posted, since=T0 + timedelta(minutes=2), backlog_max=10)
    assert [s.media_id for s in to_post] == ["m2", "m3"] and to_skip == []


def test_mark_started_ends_first_run(tmp_path):
    posted = PostedLog(tmp_path / "posts.jsonl")
    posted.mark_started()
    assert not PostedLog(tmp_path / "posts.jsonl").first_run


def test_posted_log_tolerates_bad_lines(tmp_path):
    path = tmp_path / "posts.jsonl"
    path.write_text('{"media_id": "a", "message_id": 5}\ngarbage\n{"x": 1}\n')
    assert PostedLog(path).message_id("a") == 5


# -- media choice and text ----------------------------------------------------------------------------


def test_media_prefers_fitting_video(tmp_path):
    folder = write_sidecar(tmp_path, "alice", "v", T0, files=["s.mp4", "s.jpg"], is_video=True)
    (folder / "s.mp4").write_bytes(b"0" * 100)
    (folder / "s.jpg").write_bytes(b"0" * 10)
    [sc] = scan_archive(tmp_path)
    assert choose_media(sc, 1000).path.name == "s.mp4"


def test_big_video_says_so_and_names_the_file(tmp_path):
    folder = write_sidecar(tmp_path, "alice", "v", T0, files=["s.mp4"], is_video=True)
    (folder / "s.mp4").write_bytes(b"0" * 5_000_000)
    [sc] = scan_archive(tmp_path)
    choice = choose_media(sc, 1_000_000)
    assert choice.path is None and "too large" in choice.note and "s.mp4" in choice.note


def test_big_video_falls_back_to_image_with_note(tmp_path):
    folder = write_sidecar(tmp_path, "alice", "v", T0, files=["s.mp4", "s.jpg"], is_video=True)
    (folder / "s.mp4").write_bytes(b"0" * 5_000_000)
    (folder / "s.jpg").write_bytes(b"0" * 10)
    [sc] = scan_archive(tmp_path)
    choice = choose_media(sc, 1_000_000)
    assert choice.path.name == "s.jpg" and choice.kind == "image" and "s.mp4" in choice.note


def test_no_media(tmp_path):
    write_sidecar(tmp_path, "alice", "n", T0, files=["gone.jpg"], download_error="HTTP 403")
    [sc] = scan_archive(tmp_path)
    choice = choose_media(sc, 1000)
    assert choice.path is None and "HTTP 403" in choice.note


def test_description_and_guess(tmp_path):
    write_sidecar(tmp_path, "alice", "j", T0, category="job_posting", links=["https://example.com/job"],
                  mentions=["bob"], job_title="Engineer", company="Acme")
    [sc] = scan_archive(tmp_path)
    text = story_description(sc)
    for part in ("alice", "Engineer at Acme", "https://example.com/job", "@bob", "Job posting", "rules"):
        assert part in text
    assert guess_selection(sc) == {"post_type": ["job_posting"]}
    odd = {"media_id": "q", "target": "t", "taken_at": T0.isoformat(), "category": "weird"}
    assert guess_selection(Sidecar.from_doc(odd, tmp_path)) == {}


def test_describe_tags_marks_not_applicable():
    out = describe_tags({"post_type": ["misc"], "role": None})
    assert "Misc" in out and "not applicable" in out
