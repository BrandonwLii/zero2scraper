import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from story_watch.config import ConfigError, load_config
from story_watch.quiet import next_allowed, parse_quiet_hours, parse_quiet_tz

TOR = ZoneInfo("America/Toronto")
W = parse_quiet_hours("01:00-07:00")


def at(y, mo, d, h, mi=0, tz=TOR):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


def test_outside_window_is_now():
    now = at(2026, 10, 16, 12)
    assert next_allowed(now, W, TOR) == now


def test_off_when_empty():
    now = at(2026, 10, 16, 3)
    assert parse_quiet_hours("  ") is None
    assert next_allowed(now, None, TOR) == now


def test_inside_simple_window():
    assert next_allowed(at(2026, 10, 16, 3), W, TOR) == at(2026, 10, 16, 7)
    assert next_allowed(at(2026, 10, 16, 7), W, TOR) == at(2026, 10, 16, 7)  # end is exclusive
    assert next_allowed(at(2026, 10, 16, 1), W, TOR) == at(2026, 10, 16, 7)  # start is inclusive


def test_wraps_midnight():
    w = parse_quiet_hours("22:00-06:00")
    assert next_allowed(at(2026, 10, 16, 23), w, TOR) == at(2026, 10, 17, 6)
    assert next_allowed(at(2026, 10, 17, 2), w, TOR) == at(2026, 10, 17, 6)
    assert next_allowed(at(2026, 10, 17, 12), w, TOR) == at(2026, 10, 17, 12)


def test_other_timezone_input():
    # 06:00 UTC is 02:00 in Toronto (EDT), inside the window.
    now = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
    assert next_allowed(now, W, TOR) == at(2026, 10, 16, 7)


def test_dst_spring_forward_across_window():
    # 2026-03-08: 02:00 -> 03:00 in Toronto, so 01:30 to 07:00 is 5.5 real hours.
    now = at(2026, 3, 8, 1, 30)
    end = next_allowed(now, W, TOR)
    assert end == at(2026, 3, 8, 7)
    assert end - now == timedelta(hours=5, minutes=30)


def test_dst_end_inside_gap_is_not_before_now():
    w = parse_quiet_hours("00:00-02:30")  # 02:30 does not exist on 2026-03-08
    now = at(2026, 3, 8, 1, 59)
    end = next_allowed(now, w, TOR)
    assert end >= now and end.astimezone(TOR).hour == 3


def test_dst_fall_back_window_spans_extra_hour():
    # 2026-11-01: 02:00 -> 01:00 in Toronto. The second 01:30 (fold=1) is still inside.
    now = datetime(2026, 11, 1, 1, 30, tzinfo=TOR, fold=1)
    end = next_allowed(now, W, TOR)
    assert end == at(2026, 11, 1, 7)
    assert end - now == timedelta(hours=5, minutes=30)


def test_system_local_default(monkeypatch):
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    try:
        now = datetime(2026, 10, 16, 3, 0).astimezone()
        assert next_allowed(now, W, None) == datetime(2026, 10, 16, 7, 0).astimezone()
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("raw", ["01:00-01:00", "00:00-00:00", "1-7", "25:00-07:00", "01:60-07:00", "nope", "01:00-07:00-08:00"])
def test_invalid_windows(raw):
    with pytest.raises(ValueError):
        parse_quiet_hours(raw)


def test_parse_tz():
    assert parse_quiet_tz("") is None
    assert parse_quiet_tz("America/Toronto") == TOR
    with pytest.raises(ValueError):
        parse_quiet_tz("Mars/Olympus")


def _env(**kw):
    return {"IG_USER": "u", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/secret", **kw}


def test_config_defaults_and_values():
    cfg = load_config(_env())
    assert (cfg.min_wait, cfg.max_wait, cfg.quiet_hours, cfg.quiet_tz) == (1200, 2400, None, None)
    cfg = load_config(_env(QUIET_HOURS="01:00-07:00", QUIET_TZ="America/Toronto"))
    assert cfg.quiet_hours == W and cfg.quiet_tz == TOR


@pytest.mark.parametrize("env", [{"QUIET_HOURS": "01:00-01:00"}, {"QUIET_HOURS": "x"}, {"QUIET_TZ": "Nope/Nope"}])
def test_config_errors(env):
    with pytest.raises(ConfigError):
        load_config(_env(**env))


def test_watcher_quiet_wait(cfg, store):
    from tests.test_watcher import make_watcher

    w, _, _ = make_watcher(replace(cfg, quiet_hours=W, quiet_tz=TOR, min_wait=10, max_wait=10), store)
    assert w.quiet_wait(at(2026, 10, 16, 12)) is None
    assert w.quiet_wait(at(2026, 10, 16, 6)) == 3600
    assert w.in_quiet
    assert w.quiet_wait(at(2026, 10, 16, 7)) is None
    assert not w.in_quiet


def test_watcher_quiet_wait_off(cfg, store):
    from tests.test_watcher import make_watcher

    w, _, _ = make_watcher(cfg, store)
    assert w.quiet_wait(at(2026, 10, 16, 3)) is None
