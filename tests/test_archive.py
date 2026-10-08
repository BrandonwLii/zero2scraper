import importlib.util
import io
import json
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import requests
from conftest import FakeIG, FakeNotifier, make_item
from requests.adapters import HTTPAdapter

from story_watch.archive import Archive, video_url
from story_watch.classify import Category
from story_watch.config import ConfigError, load_config
from story_watch.main import Watcher

PUBLIC = lambda host, port, proto=0: [(0, 0, 0, "", ("93.184.216.34", port))]  # noqa: E731
IMG = "https://cdn.example/1.jpg"


class Resp:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
        self.is_redirect = False

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        pass


class FakeSession:
    """Maps URL -> Resp (or an exception to raise)."""

    def __init__(self, responses):
        self.responses, self.headers, self.calls = responses, {}, []
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, **kw):
        self.calls.append((url, kw))
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r


def archive(tmp_path, responses, **kw):
    return Archive(tmp_path / "arch", session=FakeSession(responses), resolve=PUBLIC, **kw)


def item_with_nodes(media_id="1", **kw):
    item = make_item(media_id)
    return replace(item, node={"id": media_id, "sticker": "x"}, page_node={"story_link_stickers": []},
                   links=("https://jobs.example/a",), mentions=("bob",), job_title="Engineer", company="Acme", **kw)


def files(tmp_path):
    return sorted(p.name for p in (tmp_path / "arch").rglob("*") if p.is_file())


def test_image_and_sidecar_written(tmp_path):
    a = archive(tmp_path, {"https://cdn.example/1.jpg": Resp(b"JPEGDATA")})
    item = item_with_nodes("1")
    assert a.save(item, Category.JOB_POSTING, "rules")
    stem = f"{item.taken_at.strftime('%Y%m%dT%H%M%SZ')}_1"
    assert files(tmp_path) == [stem + ".jpg", stem + ".json"]
    folder = tmp_path / "arch" / "alice"
    assert (folder / (stem + ".jpg")).read_bytes() == b"JPEGDATA"
    doc = json.loads((folder / (stem + ".json")).read_text())
    assert doc["media_id"] == "1" and doc["target"] == "alice" and doc["is_video"] is False
    assert doc["links"] == ["https://jobs.example/a"] and doc["mentions"] == ["bob"]
    assert doc["job_title"] == "Engineer" and doc["company"] == "Acme"
    assert doc["category"] == "job_posting" and doc["classifier"] == "rules"
    assert doc["node"] == {"id": "1", "sticker": "x"} and doc["page_node"] == {"story_link_stickers": []}
    assert doc["taken_at"] == item.taken_at.isoformat() and doc["files"] == [stem + ".jpg"]


def test_video_story_saves_largest_video(tmp_path):
    node = {"video_resources": [{"src": "https://cdn.example/small.mp4"}, {"src": "https://cdn.example/big.mp4"}]}
    assert video_url(node) == "https://cdn.example/big.mp4"
    a = archive(tmp_path, {"https://cdn.example/big.mp4": Resp(b"MP4")})
    item = replace(make_item("2"), is_video=True, node=node)
    assert a.save(item, Category.MISC)
    names = files(tmp_path)
    assert [n.rsplit(".", 1)[1] for n in names] == ["json", "mp4"]  # poster not fetched when video worked


def test_video_over_cap_falls_back_to_image(tmp_path):
    node = {"video_resources": [{"src": "https://cdn.example/v.mp4"}]}
    a = archive(tmp_path, {"https://cdn.example/v.mp4": Resp(b"x" * 100), "https://cdn.example/2.jpg": Resp(b"IMG")},
                max_video_bytes=50)
    item = replace(make_item("2"), is_video=True, node=node)
    assert a.save(item, Category.MISC)
    assert [n.rsplit(".", 1)[1] for n in files(tmp_path)] == ["jpg", "json"]
    doc = json.loads(next((tmp_path / "arch").rglob("*.json")).read_text())
    assert doc["download_error"] == "over size cap"


def test_size_cap_holds_when_server_lies_about_length(tmp_path):
    a = archive(tmp_path, {IMG: Resp(b"x" * 1000)}, max_image_bytes=100)
    assert a.save(item_with_nodes("1"), Category.MISC)
    assert files(tmp_path)[0].endswith(".json") and len(files(tmp_path)) == 1  # no image, no .part


def test_size_cap_rejects_on_content_length(tmp_path):
    sess = FakeSession({IMG: Resp(b"", headers={"Content-Length": "999"})})
    a = Archive(tmp_path / "arch", session=sess, resolve=PUBLIC, max_image_bytes=100)
    assert a.save(item_with_nodes("1"), Category.MISC)
    assert len(files(tmp_path)) == 1


def test_total_archive_cap_evicts_oldest_but_keeps_new(tmp_path):
    a = archive(tmp_path, {f"https://cdn.example/{i}.jpg": Resp(b"x" * 400_000) for i in "123"}, max_mb=1)
    older = [replace(make_item(i, age=timedelta(hours=10 - n)), node={}) for n, i in enumerate("123")]
    for it in older:
        assert a.save(it, Category.MISC)
    left = {n.split("_")[1].split(".")[0] for n in files(tmp_path)}
    assert "3" in left and "1" not in left  # oldest went first, newest stayed
    assert sum(p.stat().st_size for p in (tmp_path / "arch").rglob("*") if p.is_file()) <= 1_000_000


def test_failed_download_still_writes_sidecar_and_never_raises(tmp_path):
    a = archive(tmp_path, {IMG: requests.ConnectionError("https://cdn.example/1.jpg?sig=SECRET")})
    assert a.save(item_with_nodes("1"), Category.MISC)
    doc = json.loads(next((tmp_path / "arch").rglob("*.json")).read_text())
    assert doc["files"] == [] and doc["download_error"] == "ConnectionError"


def test_unwritable_root_is_swallowed(tmp_path):
    blocker = tmp_path / "arch"
    blocker.write_text("a file, not a directory")
    assert Archive(blocker, session=FakeSession({}), resolve=PUBLIC).save(item_with_nodes(), Category.MISC) is False


def test_http_error_and_redirect_are_failures(tmp_path):
    for resp in (Resp(status=403), Resp(status=302)):
        a = archive(tmp_path, {IMG: resp})
        assert a.save(item_with_nodes("1"), Category.MISC)
        assert len(files(tmp_path)) == 1


def test_non_https_and_private_addresses_refused(tmp_path):
    sess = FakeSession({})
    a = Archive(tmp_path / "arch", session=sess, resolve=lambda h, p, proto=0: [(0, 0, 0, "", ("10.0.0.5", p))])
    assert a.save(item_with_nodes("1"), Category.MISC)  # private address
    a2 = Archive(tmp_path / "arch2", session=sess, resolve=PUBLIC)
    assert a2.save(replace(item_with_nodes("1"), thumbnail_url="http://cdn.example/1.jpg"), Category.MISC)
    assert sess.calls == []


def test_no_cookies_sent(tmp_path):
    seen = []

    class Capture(HTTPAdapter):
        def send(self, request, **kw):
            seen.append(dict(request.headers))
            r = requests.Response()
            r.status_code, r.raw = 200, io.BytesIO(b"IMG")
            return r

    sess = requests.Session()
    sess.mount("https://", Capture())
    sess.cookies.set("sessionid", "leftover", domain="cdn.example")  # even a stray jar entry is dropped
    Archive(tmp_path / "arch", session=sess, resolve=PUBLIC).save(item_with_nodes("1"), Category.MISC)
    assert seen and all("Cookie" not in h for h in seen)
    assert any(n.endswith(".jpg") for n in files(tmp_path))


def test_default_session_is_not_the_instaloader_one(tmp_path):
    a = Archive(tmp_path)
    assert type(a._http) is requests.Session and len(a._http.cookies) == 0


# -- watcher integration ----------------------------------------------------------------


def make_watcher(cfg, store, archive_obj):
    ig, n = FakeIG(), FakeNotifier()
    return Watcher(cfg, ig, store, n, archive=archive_obj), ig, n


def test_notification_goes_out_when_archive_blows_up(cfg, store):
    class Boom:
        def save(self, *a, **k):
            raise RuntimeError("disk on fire")

    w, ig, n = make_watcher(cfg, store, Boom())
    w.step()
    ig.items["alice"] = [make_item("1")]
    w.step()
    assert [i.media_id for i in n.stories] == ["1"] and store.is_seen("1")


def test_notification_goes_out_when_download_fails(cfg, store, tmp_path):
    a = archive(tmp_path, {IMG: requests.Timeout()})
    w, ig, n = make_watcher(cfg, store, a)
    w.step()
    ig.items["alice"] = [make_item("1")]
    w.step()
    assert [i.media_id for i in n.stories] == ["1"] and store.is_seen("1")


def test_filtered_items_are_archived_too(cfg, store, tmp_path):
    cfg = replace(cfg, notify_categories=frozenset({Category.JOB_POSTING}))
    a = archive(tmp_path, {IMG: Resp(b"IMG")})
    w, ig, n = make_watcher(cfg, store, a)
    w.step()
    ig.items["alice"] = [make_item("1")]  # no links -> misc -> filtered out
    w.step()
    assert n.stories == [] and store.is_seen("1")
    assert len(files(tmp_path)) == 2
    doc = json.loads(next((tmp_path / "arch").rglob("*.json")).read_text())
    assert doc["category"] == "misc"


def test_seeded_items_are_not_archived_and_off_by_default(cfg, store, tmp_path):
    a = archive(tmp_path, {IMG: Resp(b"IMG")})
    w, ig, n = make_watcher(cfg, store, a)
    ig.items["alice"] = [make_item("1")]
    w.step()  # first run seeds
    assert not (tmp_path / "arch").exists()
    w2, ig2, n2 = make_watcher(cfg, store, None)
    assert w2.archive is None


def test_config_archive_settings():
    env = {"IG_USER": "b", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/x"}
    assert load_config(env).archive_dir is None
    cfg = load_config({**env, "ARCHIVE_DIR": "/opt/x/archive", "ARCHIVE_MAX_MB": "10"})
    assert cfg.archive_dir == Path("/opt/x/archive") and cfg.archive_max_mb == 10


def test_config_archive_dir_must_be_absolute():
    with pytest.raises(ConfigError):
        load_config({"IG_USER": "b", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/x", "ARCHIVE_DIR": "rel"})


# -- scripts/pull_archive.py ------------------------------------------------------------


def load_pull():
    spec = importlib.util.spec_from_file_location("pull_archive", Path(__file__).parent.parent / "scripts" / "pull_archive.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, rc=0):
        self.rc, self.stdout = rc, type("S", (), {"close": lambda s: None})()

    def wait(self):
        return self.rc


def test_pull_builds_ssh_pct_exec_tar(tmp_path, monkeypatch):
    pull = load_pull()
    cmds = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: cmds.append(cmd) or FakeProc())
    dest = tmp_path / "out"
    rc = pull.pull({"PVE_HOST": "root@pve.invalid", "CTID": "120", "ARCHIVE_PULL_DIR": str(dest)})
    assert rc == 0 and dest.is_dir()
    assert cmds[0] == ["ssh", "--", "root@pve.invalid", "pct exec 120 -- tar czf - -C /opt/story-watch/archive ."]
    assert cmds[1][:3] == ["tar", "xzf", "-"] and str(dest) in cmds[1]


def test_pull_requires_pve_host_and_reports_failure(tmp_path, monkeypatch):
    pull = load_pull()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert pull.pull({"ARCHIVE_PULL_DIR": str(tmp_path)}) == 2
    assert pull.pull({"PVE_HOST": "h", "CTID": "1; rm", "ARCHIVE_PULL_DIR": str(tmp_path)}) == 2
    procs = iter([FakeProc(255), FakeProc(2)])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: next(procs))
    assert pull.pull({"PVE_HOST": "h", "ARCHIVE_PULL_DIR": str(tmp_path)}) == 1


def test_pull_default_destination(monkeypatch, tmp_path):
    pull = load_pull()
    monkeypatch.setenv("HOME", str(tmp_path))
    cmds = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: cmds.append(cmd) or FakeProc())
    assert pull.pull({"PVE_HOST": "h"}) == 0
    assert str(tmp_path / "story-watch-data" / "archive") in cmds[1]


# -- ordering: archive after delivery -----------------------------------------------------


class Events:
    def __init__(self):
        self.log = []


class OrderNotifier(FakeNotifier):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def story(self, item, category=None, roles=()):
        self.events.log.append(("story", item.media_id))
        super().story(item, category=category, roles=roles)


class RecordingArchive:
    def __init__(self, events, raises=False):
        self.events, self.raises, self.saved = events, raises, []

    def save(self, item, category, classifier=""):
        self.events.log.append(("save", item.media_id))
        self.saved.append((item.media_id, category))
        if self.raises:
            raise RuntimeError("slow and broken")


def ordered(cfg, store, **kw):
    ev = Events()
    ig, n = FakeIG(), OrderNotifier(ev)
    return Watcher(cfg, ig, store, n, archive=RecordingArchive(ev, **kw)), ig, n, ev


def test_all_stories_sent_before_any_archive_save(cfg, store):
    w, ig, n, ev = ordered(cfg, store)
    w.step()
    ig.items["alice"] = [make_item("1"), make_item("2", age=timedelta(minutes=1))]
    ig.links = {"1": ("https://jobs.example/a",)}
    w.step()
    kinds = [k for k, _ in ev.log]
    assert kinds == ["story", "story", "save", "save"]
    assert sorted(m for m, _ in w.archive.saved) == ["1", "2"]


def test_raising_archive_cannot_hold_back_any_notification(cfg, store):
    w, ig, n, ev = ordered(cfg, store, raises=True)
    w.step()
    ig.items["alice"] = [make_item("1"), make_item("2", age=timedelta(minutes=1))]
    w.step()
    assert sorted(i.media_id for i in n.stories) == ["1", "2"]
    assert store.is_seen("1") and store.is_seen("2")


def test_filtered_and_unseen_items_are_archived_with_their_category(cfg, store):
    cfg = replace(cfg, notify_categories=frozenset({Category.JOB_POSTING}))
    w, ig, n, ev = ordered(cfg, store)
    w.step()
    ig.items["alice"] = [make_item("1", age=timedelta(minutes=2)), make_item("2")]
    ig.links = {"2": ("https://jobs.example/a",)}
    n.fail = True  # item 2 (job posting) fails to deliver; item 1 (misc) is filtered
    with pytest.raises(Exception):
        w.run_cycle()
    assert not store.is_seen("2") and store.is_seen("1")
    assert dict(w.archive.saved) == {"1": Category.MISC, "2": Category.JOB_POSTING}


def test_archive_runs_once_per_item_without_reclassifying(cfg, store):
    calls = []

    class Counting:
        def classify(self, item):
            calls.append(item.media_id)
            return Category.MISC

    ev = Events()
    ig, n = FakeIG(), OrderNotifier(ev)
    w = Watcher(cfg, ig, store, n, archive=RecordingArchive(ev), classifier=Counting())
    w.step()
    ig.items["alice"] = [make_item("1")]
    w.step()
    assert calls == ["1"] and len(w.archive.saved) == 1
