import os

from story_watch.bot import relogin as R
from story_watch.bot.config import BOT_ENV_KEYS, DEFAULT_RELOGIN_DIR, DEFAULT_RELOGIN_URL, load_bot_config
from story_watch.bot.outage import pending_dms
from story_watch.store import Outage

from test_bot_env import BOT


def test_write_request_atomic_and_debounced(tmp_path):
    assert R.write_request(tmp_path, "start", now=1000.0) == "ok"
    assert (tmp_path / "request").read_text() == "start\n"
    assert [p.name for p in tmp_path.iterdir()] == ["request"]
    mtime = (tmp_path / "request").stat().st_mtime
    assert R.write_request(tmp_path, "done", now=mtime + 60) == "busy"
    assert (tmp_path / "request").read_text() == "start\n"
    assert R.write_request(tmp_path, "done", now=mtime + 121) == "ok"
    assert (tmp_path / "request").read_text() == "done\n"


def test_write_request_rejects_bad_action_and_missing_dir(tmp_path):
    assert R.write_request(tmp_path, "rm -rf") == "error"
    assert R.write_request(tmp_path / "nope", "start") == "error"


def test_write_request_after_host_consumed_it(tmp_path):
    assert R.write_request(tmp_path, "start") == "ok"
    (tmp_path / "request").unlink()
    assert R.write_request(tmp_path, "done") == "ok"


def test_parse_status():
    s = R.parse_status("failed 1700000000 no Instagram login in the login browser\n")
    assert (s.state, s.epoch, s.message) == ("failed", 1700000000, "no Instagram login in the login browser")
    assert R.parse_status("imported 5").message == ""
    assert R.parse_status("bogus 5 x") is None
    assert R.parse_status("failed x y") is None
    assert R.parse_status("") is None
    assert len(R.parse_status("failed 1 " + "a" * 500).message) == R.MESSAGE_MAX


def test_failed_dm_dedupe(tmp_path):
    assert R.failed_to_report(tmp_path) is None  # no status file
    (tmp_path / "status").write_text("browser_ready 100 ok\n")
    assert R.failed_to_report(tmp_path) is None
    (tmp_path / "status").write_text("failed 200 import failed\n")
    s = R.failed_to_report(tmp_path)
    assert R.failed_text(s) == "Re-login failed: import failed"
    R.mark_reported(tmp_path, s.epoch)
    assert R.failed_to_report(tmp_path) is None
    (tmp_path / "status").write_text("failed 300 again\n")
    assert R.failed_to_report(tmp_path).epoch == 300


def test_owner_only():
    assert R.is_owner(5, 5)
    assert not R.is_owner(6, 5)
    assert not R.is_owner(5, None)


def test_config_relogin(tmp_path):
    base = {**BOT, "ARCHIVE_DIR": str(tmp_path)}
    cfg = load_bot_config(base)
    assert str(cfg.relogin_dir) == DEFAULT_RELOGIN_DIR and cfg.relogin_url == DEFAULT_RELOGIN_URL
    assert load_bot_config({**base, "RELOGIN_DIR": ""}).relogin_dir is None
    cfg = load_bot_config({**base, "RELOGIN_DIR": str(tmp_path), "RELOGIN_URL": "https://x.example/v"})
    assert cfg.relogin_dir == tmp_path and cfg.relogin_url == "https://x.example/v"
    assert {"RELOGIN_DIR", "RELOGIN_URL"} <= set(BOT_ENV_KEYS)


def test_dms_carry_buttons_and_new_text():
    base = dict(started_at=0.0, failures=3, last_error_type="SessionError", alert_due_at=1.0)
    (dm,) = pending_dms(Outage(kind="session", **base), 10.0)
    assert dm.relogin_buttons and "Start login browser" in dm.body and "I've logged in" in dm.body
    assert "refresh_session.sh" in dm.body
    (dm,) = pending_dms(Outage(kind="flagged", **base), 10.0)
    assert dm.relogin_buttons
    (dm,) = pending_dms(Outage(kind="other", **base), 10.0)
    assert not dm.relogin_buttons
    dms = pending_dms(Outage(kind="other", relogin_failed=True, dm_sent_at=2.0, **base), 10.0)
    assert dms[0].field == "relogin_dm_sent_at" and dms[0].relogin_buttons
