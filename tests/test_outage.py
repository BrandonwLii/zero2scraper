import os
from dataclasses import replace

import pytest

from story_watch.bot.config import BOT_ENV_KEYS, BotConfigError, load_bot_config
from story_watch.bot.outage import pending_dms
from story_watch.config import ConfigError
from story_watch.instagram import InstagramError, SessionError
from story_watch.main import CHANNEL_FALLBACK_AFTER, classify_failure
from story_watch.notify import DiscordError
from story_watch.store import Outage, Store

from conftest import make_item
from test_watcher import make_watcher


def test_store_outage_lifecycle():
    s = Store(":memory:")
    assert s.outage() is None
    s.record_failure("other", "KeyError", 100.0, 3)
    o = s.outage()
    assert (o.kind, o.failures, o.started_at, o.alert_due_at) == ("other", 1, 100.0, None)
    s.record_failure("other", "KeyError", 110.0, 3)
    s.record_failure("session", "SessionError", 120.0, 3)  # escalates; alert due at failure 3
    o = s.outage()
    assert (o.kind, o.failures, o.alert_due_at, o.last_error_type) == ("session", 3, 120.0, "SessionError")
    s.record_failure("session", "SessionError", 130.0, 3)
    assert s.outage().alert_due_at == 120.0  # set once
    s.mark_relogin_failed()
    s.mark_dm_sent("dm_sent_at", 125.0)
    s.resolve(200.0, 4)
    o = s.outage()
    assert o.relogin_failed and o.dm_sent_at == 125.0 and o.resolved_at == 200.0 and o.caught_up == 4
    s.record_failure("flagged", "InstagramError", 300.0, 3)  # a new outage replaces the resolved row
    o = s.outage()
    assert (o.kind, o.failures, o.started_at, o.resolved_at, o.dm_sent_at, o.relogin_failed) == (
        "flagged", 1, 300.0, None, None, False)
    with pytest.raises(ValueError):
        s.mark_dm_sent("kind", 1.0)


def test_classify_failure():
    assert classify_failure(SessionError("x")) == "session"
    for msg in ("feedback_required", "HTTP 429", "Please wait a few minutes", "Rate limit hit"):
        assert classify_failure(InstagramError(msg)) == "flagged"
    assert classify_failure(InstagramError("boom")) == "other"
    assert classify_failure(DiscordError("429")) == "other"
    assert classify_failure(RuntimeError("429")) == "other"


def test_threshold_and_resolve_caught_up(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    store.seed("alice", [])
    ig.error = InstagramError("boom")
    w.step()
    w.step()
    assert store.outage().alert_due_at is None
    w.step()
    assert store.outage().alert_due_at is not None and store.outage().failures == 3
    ig.error = None
    ig.items["alice"] = [make_item("1"), make_item("2")]
    w.step()
    o = store.outage()
    assert o.resolved_at is not None and o.caught_up == 2


def test_session_error_is_due_at_once(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.error = SessionError("checkpoint_required")
    w.step()
    o = store.outage()
    assert o.kind == "session" and o.alert_due_at is not None and o.last_error_type == "SessionError"


def test_relogin_failed_when_session_file_is_newer(cfg, store, tmp_path):
    path = tmp_path / "session"
    path.write_text("x")
    os.utime(path, (1, 1))
    w, ig, n = make_watcher(replace(cfg, ig_session_path=str(path)), store)
    ig.error = SessionError("login")
    w.step()
    assert not store.outage().relogin_failed
    os.utime(path)  # a fresh login was pushed
    w.step()
    assert store.outage().relogin_failed


def test_db_error_never_breaks_the_loop(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.error = InstagramError("x")
    store.record_failure = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
    assert w.step() > 0


def test_channel_alerts_unchanged_without_alert_user_id(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.error = InstagramError("down")
    for _ in range(3):
        w.step()
    assert n.alerts == ["Story watcher failing"]


def test_bot_dm_recorded_means_no_channel_post(cfg, store):
    clock = [1000.0]
    w, ig, n = make_watcher(replace(cfg, alert_user_id="100000000000000001"), store)
    w.wall = lambda: clock[0]
    ig.error = InstagramError("down")
    for _ in range(3):
        w.step()
    assert n.alerts == []  # the bot gets the first 15 minutes
    store.mark_dm_sent("dm_sent_at", clock[0])
    clock[0] += CHANNEL_FALLBACK_AFTER + 60
    w.step()
    assert n.alerts == []
    ig.error = None
    w.step()
    assert n.alerts == []  # no channel alert, so no channel "recovered"


def test_no_dm_for_15_minutes_sends_one_channel_post_then_recovered(cfg, store):
    clock = [1000.0]
    w, ig, n = make_watcher(replace(cfg, alert_user_id="100000000000000001"), store)
    w.wall = lambda: clock[0]
    ig.error = InstagramError("down")
    for _ in range(3):
        w.step()
    assert n.alerts == []
    clock[0] += CHANNEL_FALLBACK_AFTER + 1
    w.step()
    w.step()
    assert n.alerts == ["Story watcher failing"]
    assert store.outage().channel_fallback_sent_at is not None
    ig.error = None
    w.step()
    assert n.alerts == ["Story watcher failing", "Story watcher recovered"]


def test_alert_user_id_validation():
    from story_watch.config import load_config
    base = {"IG_USER": "b", "TARGETS": "a:1", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/x"}
    assert load_config({**base, "ALERT_USER_ID": "100000000000000001"}).alert_user_id == "100000000000000001"
    assert load_config({**base, "ALERT_USER_ID": ""}).alert_user_id == ""
    for bad in ("12345", "abc00000000000000001", "1" * 21):
        with pytest.raises(ConfigError):
            load_config({**base, "ALERT_USER_ID": bad})


def test_bot_config_alert_user_id(tmp_path):
    env = {"DISCORD_BOT_TOKEN": "t", "LABEL_CHANNEL_ID": "100000000000000001",
           "LABELER_USER_IDS": "100000000000000002", "ARCHIVE_DIR": str(tmp_path)}
    assert "ALERT_USER_ID" in BOT_ENV_KEYS
    assert load_bot_config(env).alert_user_id is None
    assert load_bot_config({**env, "ALERT_USER_ID": "100000000000000003"}).alert_user_id == 100000000000000003
    with pytest.raises(BotConfigError):
        load_bot_config({**env, "ALERT_USER_ID": "nope"})


def O(**kw):
    base = dict(kind="other", started_at=1000.0, failures=3, last_error_type="KeyError", alert_due_at=1100.0)
    return Outage(**{**base, **kw})


def test_pending_dms_branches():
    assert pending_dms(None, 2000.0) == []
    assert pending_dms(O(alert_due_at=None), 2000.0) == []
    (d,) = pending_dms(O(kind="session"), 2000.0)
    assert d.field == "dm_sent_at" and d.title == "Instagram signed the burner out"
    assert d.link_url == "https://www.instagram.com/accounts/login/" and "<t:1000:R>" in d.body and "3" in d.body
    (d,) = pending_dms(O(kind="flagged"), 2000.0)
    assert d.title == "Instagram is throttling the burner" and d.link_url == "https://www.instagram.com/"
    (d,) = pending_dms(O(), 2000.0)
    assert d.title == "Story watcher failing" and d.link_url is None and "`KeyError`" in d.body
    assert "3 consecutive failures" in d.body
    assert pending_dms(O(dm_sent_at=1200.0), 2000.0) == []
    (d,) = pending_dms(O(dm_sent_at=1200.0, relogin_failed=True), 2000.0)
    assert d.field == "relogin_dm_sent_at" and d.title == "Still failing after a fresh login"
    assert pending_dms(O(dm_sent_at=1.0, relogin_failed=True, relogin_dm_sent_at=2.0), 2000.0) == []
    # resolved: recovered DM only if the alert DM went out
    assert pending_dms(O(resolved_at=5800.0, caught_up=2), 6000.0) == []
    (d,) = pending_dms(O(dm_sent_at=1200.0, resolved_at=5800.0, caught_up=2), 6000.0)
    assert d.field == "recovered_dm_sent_at" and d.title == "Story watcher back online"
    assert "Down for 1 h 20 min; 2 stories caught up." == d.body
    assert pending_dms(O(dm_sent_at=1200.0, resolved_at=5800.0, caught_up=2, recovered_dm_sent_at=5900.0), 6000.0) == []
