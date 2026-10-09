from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import JOB_TAGS, MISC_TAGS, FakeIG, FakeNotifier, make_item

from story_watch.tags import PostType, Tags
from story_watch.instagram import InstagramError, SessionError
from story_watch.pings import PingPrefs
from story_watch.main import BACKOFF_CAP, Backoff, Watcher


def make_watcher(cfg, store):
    ig, notifier = FakeIG(), FakeNotifier()
    return Watcher(cfg, ig, store, notifier), ig, notifier


def test_first_run_seeds_without_notifying(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.items["alice"] = [make_item("1"), make_item("2")]
    w.step()
    assert n.stories == []
    assert store.is_seen("1") and store.is_seen("2")
    assert store.is_seeded("alice")


def test_first_run_with_no_stories_still_seeds(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    w.step()  # no stories at all
    ig.items["alice"] = [make_item("1")]
    w.step()
    assert [i.media_id for i in n.stories] == ["1"]


def test_new_item_notifies_once_and_is_marked_seen(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.items["alice"] = [make_item("1")]
    w.step()
    ig.items["alice"] = [make_item("1"), make_item("2")]
    w.step()
    w.step()
    assert [i.media_id for i in n.stories] == ["2"]
    assert store.is_seen("2")


def test_failed_discord_send_is_retried_next_cycle(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    w.step()
    ig.items["alice"] = [make_item("1")]
    n.fail = True
    w.step()
    assert not store.is_seen("1")
    n.fail = False
    w.step()
    assert [i.media_id for i in n.stories] == ["1"]
    assert store.is_seen("1")


def test_backoff_doubles_caps_and_resets():
    b = Backoff()
    waits = [b.fail(300) for _ in range(6)]
    assert waits == [600, 1200, 2400, BACKOFF_CAP, BACKOFF_CAP, BACKOFF_CAP]
    b.reset()
    assert b.current is None
    assert b.fail(300) == 600


def test_watcher_backoff_resets_on_success(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.error = InstagramError("429")
    first = w.step()
    second = w.step()
    assert 600 <= first <= 1200
    assert second == min(BACKOFF_CAP, first * 2)
    ig.error = None
    ok = w.step()
    assert 300 <= ok <= 600
    assert w.backoff.current is None


def test_generic_alert_once_then_recovered(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.error = InstagramError("down")
    for _ in range(5):
        w.step()
    assert n.alerts == ["Story watcher failing"]
    ig.error = None
    w.step()
    assert n.alerts == ["Story watcher failing", "Story watcher recovered"]


def test_session_error_alerts_once_and_distinctly(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.error = SessionError("checkpoint_required")
    for _ in range(5):
        w.step()
    assert n.alerts == ["Instagram session needs re-login"]


def test_heartbeat_once_per_day(cfg, store):
    from dataclasses import replace

    w, _, n = make_watcher(replace(cfg, heartbeat_hour=9), store)
    t = datetime(2026, 1, 1, 9, 5)
    w.maybe_heartbeat(t)
    w.maybe_heartbeat(t + timedelta(minutes=10))
    w.maybe_heartbeat(t + timedelta(hours=1))
    w.maybe_heartbeat(t + timedelta(days=1))
    assert n.alerts == ["Story watcher heartbeat"] * 2


class FakeJobs:
    def __init__(self, titles):
        self.titles = titles
        self.calls = []

    def lookup(self, url):
        from story_watch.jobs import JobInfo

        self.calls.append(url)
        return JobInfo(self.titles.get(url), "Acme" if url in self.titles else None)


JOB = "https://job-boards.greenhouse.io/acme/jobs/1"


def test_new_items_get_links_and_job_titles(cfg, store):
    jobs = FakeJobs({JOB: "SWE Intern"})
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, jobs=jobs)
    w.step()  # seed
    ig.items["alice"] = [make_item("1"), make_item("2")]
    ig.links = {"1": (JOB,), "2": ("https://youtube.com/@x",)}
    w.step()
    by_id = {i.media_id: i for i in n.stories}
    assert by_id["1"].job_title == "SWE Intern" and by_id["1"].company == "Acme"
    assert by_id["2"].links == ("https://youtube.com/@x",) and by_id["2"].job_title is None
    assert jobs.calls == [JOB]  # non-job links aren't fetched


def test_extras_only_fetched_when_something_is_new(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    ig.items["alice"] = [make_item("1")]
    w.step()  # seed
    w.step()  # nothing new
    assert ig.extras_calls == 0


def test_extras_failure_still_notifies(cfg, store):
    w, ig, n = make_watcher(cfg, store)
    w.step()
    ig.items["alice"] = [make_item("1")]
    ig.extras_error = InstagramError("story page: HTTP 429")
    w.step()
    assert [i.media_id for i in n.stories] == ["1"] and n.stories[0].links == ()
    assert w.consecutive_failures == 0


class FixedClassifier:
    def __init__(self, by_id, error=None):
        self.by_id, self.error = by_id, error

    def classify(self, item):
        if self.error:
            raise self.error
        return self.by_id.get(item.media_id, MISC_TAGS)


U1, U2, U3 = "111111111111111111", "222222222222222222", "333333333333333333"


def _prefs(*keys):
    return PingPrefs.from_keys(list(keys))


def test_filtered_post_type_is_recorded_but_not_sent(cfg, store):
    cfg = replace(cfg, notify_post_types=frozenset({PostType.JOB_POSTING}))
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({"1": JOB_TAGS}))
    w.step()
    ig.items["alice"] = [make_item("1"), make_item("2")]
    w.step()
    w.step()
    assert [i.media_id for i in n.stories] == ["1"]
    assert store.is_seen("2")  # not retried every cycle


def _run_one(cfg, store, tags, notifier=None, item_id="1"):
    ig, n = FakeIG(), notifier or FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({item_id: tags}))
    w.step()
    ig.items["alice"] = [make_item(item_id)]
    w.step()
    return w, n


def test_matching_users_are_pinged_from_the_stored_prefs(cfg, store):
    store.set_ping_prefs(U1, _prefs("post_type:job_posting"))
    store.set_ping_prefs(U2, _prefs("post_type:event"))
    store.set_ping_prefs(U3, PingPrefs())  # saved but empty: never pinged
    _, n = _run_one(cfg, store, JOB_TAGS)
    assert n.sent[0][2] == (U1,)


def test_unsure_story_pings_everyone_who_could_match(cfg, store):
    store.set_ping_prefs(U1, _prefs("post_type:event"))
    store.set_ping_prefs(U2, _prefs("post_type:misc"))
    store.set_ping_prefs(U3, _prefs("post_type:job_posting", "role:pm"))
    _, n = _run_one(cfg, store, Tags.unsure())
    assert n.sent[0][2] == (U1, U2, U3)


def test_no_users_no_pings(cfg, store):
    _, n = _run_one(cfg, store, JOB_TAGS)
    assert n.sent[0][2] == ()


def test_pings_use_the_stored_tags_not_a_fresh_classification(cfg, store):
    class Flaky:
        calls = 0

        def classify(self, item):
            self.calls += 1
            return JOB_TAGS if self.calls == 1 else MISC_TAGS

    store.set_ping_prefs(U1, _prefs("post_type:job_posting"))
    store.set_ping_prefs(U2, _prefs("post_type:misc"))
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=Flaky())
    w.step()
    ig.items["alice"] = [make_item("1")]
    n.fail = True
    w.step()
    n.fail = False
    w.step()
    assert n.sent[0][2] == (U1,)


def test_unreadable_prefs_post_without_pings_and_mark_seen(cfg, store, caplog, monkeypatch):
    def corrupt():
        raise ValueError("bad row role:wizard")

    monkeypatch.setattr(store, "all_ping_prefs", corrupt)
    with caplog.at_level("WARNING"):
        w, n = _run_one(cfg, store, JOB_TAGS)
    assert n.sent[0][2] == () and store.is_seen("1") and w.consecutive_failures == 0
    assert "ping preferences unreadable (ValueError)" in caplog.text
    assert "wizard" not in caplog.text


def test_locked_database_fails_open(cfg, store, monkeypatch):
    import sqlite3

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "all_ping_prefs", locked)
    w, n = _run_one(cfg, store, JOB_TAGS)
    assert n.sent[0][2] == () and store.is_seen("1")


def test_filtered_story_reads_no_prefs_and_pings_nobody(cfg, store, monkeypatch):
    cfg = replace(cfg, notify_post_types=frozenset({PostType.JOB_POSTING}))
    monkeypatch.setattr(store, "all_ping_prefs", lambda: pytest.fail("prefs read for a filtered story"))
    _, n = _run_one(cfg, store, MISC_TAGS)
    assert n.sent == [] and store.is_seen("1")


def test_failed_main_post_pings_nobody_and_a_retry_pings_once(cfg, store):
    """The item is re-posted only if the main post failed, so no user is pinged twice."""
    store.set_ping_prefs(U1, _prefs("post_type:job_posting"))
    n = FakeNotifier()
    n.fail = True
    w, _ = _run_one(cfg, store, JOB_TAGS, notifier=n)
    assert n.sent == [] and not store.is_seen("1")
    n.fail = False
    w.step()
    w.step()
    w.step()
    assert [s[2] for s in n.sent] == [(U1,)] and store.is_seen("1")


def test_alerts_carry_no_mentions():
    from unittest import mock

    from story_watch.notify import Notifier

    posted = []
    http = mock.Mock()
    http.post.side_effect = lambda url, json=None, **kw: posted.append(json) or mock.Mock(status_code=204)
    Notifier("https://discord.com/api/webhooks/1/secret", session=http).alert("t", "d")
    assert posted[0]["allowed_mentions"] == {"parse": []} and "content" not in posted[0]


def test_classifier_failure_falls_back_to_unsure(cfg, store):
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({}, error=RuntimeError("llm down")))
    w.step()
    ig.items["alice"] = [make_item("1")]
    w.step()
    assert n.sent[0][1] == Tags.unsure()
    assert w.consecutive_failures == 0


def test_unsure_story_is_posted_even_with_filters(cfg):
    """Fail open: only a story whose every possible post type is filtered is dropped."""
    cfg = replace(cfg, notify_post_types=frozenset({PostType.JOB_POSTING}))
    assert cfg.should_notify(Tags(post_type=[PostType.JOB_POSTING, PostType.EVENT])) and cfg.should_notify(Tags.unsure())
    assert not cfg.should_notify(Tags(post_type=[PostType.EVENT, PostType.MISC]))


def test_retry_reuses_the_stored_tags(cfg, store):
    class Flaky:
        """Nondeterministic: a different answer on every call."""
        calls = 0

        def classify(self, item):
            self.calls += 1
            return JOB_TAGS if self.calls == 1 else MISC_TAGS

    ig, n, c = FakeIG(), FakeNotifier(), Flaky()
    w = Watcher(cfg, ig, store, n, classifier=c)
    w.step()
    ig.items["alice"] = [make_item("1")]
    n.fail = True
    w.step()
    assert not store.is_seen("1") and store.get_tags("1") == JOB_TAGS
    n.fail = False
    w.step()
    w.step()
    assert c.calls == 1 and n.sent[-1][1] == JOB_TAGS and store.is_seen("1")


def test_failed_post_is_retried_alone_without_reposting_the_rest(cfg, store):
    class FailsOn(FakeNotifier):
        def story(self, item, tags=None, user_ids=()):
            self.fail = item.media_id == "2" and self.broken
            super().story(item, tags, user_ids)

    ig, n = FakeIG(), FailsOn()
    n.broken = True
    w = Watcher(cfg, ig, store, n)
    w.step()
    ig.items["alice"] = [make_item("1"), make_item("2"), make_item("3")]
    w.step()
    assert [i.media_id for i in n.stories] == ["1"]  # 3 waits behind the failure, in order
    assert store.is_seen("1") and not store.is_seen("2") and not store.is_seen("3")
    assert w.consecutive_failures == 1
    n.broken = False
    w.step()
    assert [i.media_id for i in n.stories] == ["1", "2", "3"]  # 1 not posted twice
    assert all(store.is_seen(m) for m in "123") and w.consecutive_failures == 0
