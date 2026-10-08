from datetime import datetime, timedelta

from conftest import FakeIG, FakeNotifier, make_item

from story_watch.instagram import InstagramError, SessionError
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
