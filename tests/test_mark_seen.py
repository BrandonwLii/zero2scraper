"""MARK_SEEN (#50): request building, reply checks, the client call (mocked HTTP) and the watcher."""

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest
import requests
from conftest import FakeIG, FakeNotifier, make_item

from story_watch import instagram
from story_watch.config import load_config
from story_watch.instagram import (
    SEEN_DOC_ID,
    SEEN_URL,
    InstagramClient,
    InstagramError,
    SessionError,
    WebTokens,
    build_seen_request,
    check_seen_response,
    web_tokens_from_page,
)
from story_watch.main import Watcher
from story_watch.notify import DiscordError
from story_watch.tags import PostType

DTSG = "NAfvSECRETDTSG:17:1700000000"
PAGE_HTML = (
    '<script>requireLazy(["DTSGInitialData",[],{"token":"' + DTSG + '"},258],'
    '["LSD",[],{"token":"AVqLSDtoken"},323],{"X-IG-App-ID":"936619743392459"},'
    '{"__spin_r":1012345678,"__spin_b":"trunk","__spin_t":1760000000,"hsi":"7512345678901234567",'
    '"haste_session":"20370.HYP:instagram_web_pkg.2.1...0"})</script>'
)
OK_REPLY = 'for (;;);{"data":{"xdt_mark_story_reel_seen":{"__typename":"XDTMarkSeenResponse"}},"extensions":{}}'
TAKEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def tokens():
    return web_tokens_from_page(PAGE_HTML)


# --- tokens -----------------------------------------------------------------------------------

def test_tokens_from_page():
    t = tokens()
    assert (t.fb_dtsg, t.lsd, t.app_id) == (DTSG, "AVqLSDtoken", "936619743392459")
    assert (t.spin_r, t.spin_b, t.spin_t, t.hsi) == ("1012345678", "trunk", "1760000000", "7512345678901234567")
    assert DTSG not in repr(t) and "AVqLSDtoken" not in repr(t)


def test_tokens_need_fb_dtsg_and_default_app_id():
    assert web_tokens_from_page("<html>logged out</html>") is None
    t = web_tokens_from_page('"DTSGInitialData",[],{"token":"x"}')
    assert t.app_id == instagram.DEFAULT_APP_ID and t.lsd == ""


def test_token_scan_is_linear_on_hostile_pages():
    import time

    html = '"DTSGInitialData",[],{"token":"' * 20000 + "a" * 200000
    start = time.monotonic()
    assert web_tokens_from_page(html) is None
    assert time.monotonic() - start < 2


# --- request ----------------------------------------------------------------------------------

def test_build_seen_request_is_the_web_mutation():
    req = build_seen_request(
        target="alice", media_id="3700000000000000001", owner_id=7, taken_at=TAKEN,
        viewed_at=1760620000, viewer_id="99", csrftoken="csrfX", tokens=tokens(),
    )
    assert req.url == SEEN_URL == "https://www.instagram.com/api/graphql"
    d = req.data
    assert d["doc_id"] == SEEN_DOC_ID
    assert d["fb_api_req_friendly_name"] == "PolarisStoriesV3SeenMutation"
    assert d["fb_dtsg"] == DTSG and d["lsd"] == "AVqLSDtoken" and d["av"] == "99"
    assert d["jazoest"] == "2" + str(sum(map(ord, DTSG)))
    variables = json.loads(d["variables"])
    assert list(variables) == ["reelId", "reelMediaId", "reelMediaOwnerId", "reelMediaTakenAt", "viewSeenAt"]
    assert variables == {
        "reelId": "7", "reelMediaId": "3700000000000000001", "reelMediaOwnerId": "7",
        "reelMediaTakenAt": int(TAKEN.timestamp()), "viewSeenAt": 1760620000,
    }
    h = req.headers
    assert h["X-CSRFToken"] == "csrfX" and h["X-IG-App-ID"] == "936619743392459"
    assert h["X-FB-LSD"] == "AVqLSDtoken" and h["X-FB-Friendly-Name"] == "PolarisStoriesV3SeenMutation"
    assert h["Content-Type"] == "application/x-www-form-urlencoded"
    assert h["Sec-Fetch-Site"] == "same-origin" and h["Origin"] == "https://www.instagram.com"
    assert h["Referer"] == "https://www.instagram.com/stories/alice/3700000000000000001/"
    # Secrets stay out of repr (and so out of logs and exception messages).
    assert DTSG not in repr(req) and "csrfX" not in repr(req)


def test_build_seen_request_is_pure():
    kw = dict(target="a", media_id="1", owner_id="7", taken_at=TAKEN, viewed_at=5,
              viewer_id="9", csrftoken="c", tokens=tokens())
    assert build_seen_request(**kw) == build_seen_request(**kw)


# --- reply ------------------------------------------------------------------------------------

def test_ok_reply():
    check_seen_response(200, "", OK_REPLY)
    check_seen_response(200, "", OK_REPLY.removeprefix("for (;;);"))


@pytest.mark.parametrize("status,location,text", [
    (302, "https://www.instagram.com/accounts/login/?next=/", ""),
    (302, "/challenge/action/", ""),
    (400, "", '{"message":"checkpoint_required","checkpoint_url":"/challenge/x","status":"fail"}'),
    (401, "", '{"message":"login_required","status":"fail"}'),
    (200, "", '{"message":"","checkpoint_url":"https://www.instagram.com/challenge/1/","status":"fail"}'),
])
def test_session_replies(status, location, text):
    with pytest.raises(SessionError) as e:
        check_seen_response(status, location, text)
    assert "instagram.com" not in str(e.value) and "challenge/" not in str(e.value)


@pytest.mark.parametrize("status,location,text", [
    (429, "", '{"message":"Please wait a few minutes","status":"fail"}'),
    (500, "", "oops"),
    (302, "https://www.instagram.com/", ""),
    (200, "", "<html>shell with login_required and checkpoint in its scripts</html>"),
    (200, "", '{"data":{"xdt_mark_story_reel_seen":null},"errors":[{"message":"x"}]}'),
    (200, "", '{"errors":[{"message":"bad doc_id"}]}'),
    (200, "", "[]"),
])
def test_failed_replies_are_instagram_errors(status, location, text):
    with pytest.raises(InstagramError) as e:
        check_seen_response(status, location, text)
    assert not isinstance(e.value, SessionError)
    assert "wait" not in str(e.value) and "doc_id" not in str(e.value)  # never the body


# --- client (mocked HTTP) ---------------------------------------------------------------------

class Resp:
    def __init__(self, text=OK_REPLY, status=200, url="https://www.instagram.com/stories/alice/", headers=None):
        self.text, self.status_code, self.url, self.headers = text, status, url, headers or {}


class Http:
    def __init__(self, page=PAGE_HTML, posts=None):
        self.headers = {"Upgrade-Insecure-Requests": "1"}
        self.cookies = {"csrftoken": "csrfX", "ds_user_id": "99", "sessionid": "SESSIONSECRET"}
        self.page = page
        self.posts = list(posts or [])
        self.sent = []

    def get(self, url, **kw):
        return Resp(self.page)

    def post(self, url, **kw):
        self.sent.append((url, kw))
        nxt = self.posts.pop(0) if self.posts else Resp()
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class Loader:
    context = object()

    def load_session_from_file(self, user, path):
        pass


def client(http):
    return InstagramClient("b", loader=Loader(), userids={"alice": 7}, page_session=http)


def test_mark_seen_posts_one_mutation_per_item_in_order():
    http = Http()
    c = client(http)
    c.story_page_items("alice")
    items = [make_item("11"), make_item("12")]
    assert c.mark_seen("alice", items, now=1760620000) == 2
    assert [json.loads(kw["data"]["variables"])["reelMediaId"] for _, kw in http.sent] == ["11", "12"]
    url, kw = http.sent[0]
    assert url == SEEN_URL
    assert kw["allow_redirects"] is False and kw["timeout"] == instagram.SEEN_TIMEOUT
    assert kw["headers"]["Upgrade-Insecure-Requests"] is None  # dropped by requests' merge
    assert kw["headers"]["X-CSRFToken"] == "csrfX" and kw["data"]["av"] == "99"
    assert json.loads(kw["data"]["variables"])["viewSeenAt"] == 1760620000


def test_mark_seen_through_a_real_requests_session_drops_the_navigation_header(monkeypatch):
    seen = {}

    def send(self, request, **kw):
        seen["headers"] = dict(request.headers)
        seen["body"] = parse_qs(request.body)
        r = requests.Response()
        r.status_code, r._content, r.url = 200, OK_REPLY.encode(), request.url
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    s = requests.Session()
    s.headers.update({"Upgrade-Insecure-Requests": "1", "Sec-Fetch-Mode": "navigate"})
    s.cookies.set("csrftoken", "csrfX", domain=".instagram.com")
    s.cookies.set("ds_user_id", "99", domain=".instagram.com")
    c = InstagramClient("b", loader=Loader(), userids={"alice": 7}, page_session=s)
    c._tokens = tokens()
    assert c.mark_seen("alice", [make_item("11")]) == 1
    assert "Upgrade-Insecure-Requests" not in seen["headers"]
    assert seen["headers"]["Sec-Fetch-Mode"] == "cors"
    assert seen["body"]["doc_id"] == [SEEN_DOC_ID]


def test_mark_seen_without_page_tokens_sends_nothing():
    http = Http(page="<html>no tokens</html>")
    c = client(http)
    c.story_page_items("alice")
    with pytest.raises(InstagramError):
        c.mark_seen("alice", [make_item("11")])
    assert http.sent == []


def test_mark_seen_without_csrf_cookie_is_a_session_error():
    http = Http()
    c = client(http)
    c.story_page_items("alice")
    http.cookies.pop("csrftoken")
    with pytest.raises(SessionError):
        c.mark_seen("alice", [make_item("11")])
    assert http.sent == []


def test_mark_seen_stops_at_the_first_failure():
    http = Http(posts=[Resp(status=500), Resp()])
    c = client(http)
    c.story_page_items("alice")
    with pytest.raises(InstagramError):
        c.mark_seen("alice", [make_item("11"), make_item("12")])
    assert len(http.sent) == 1


def test_mark_seen_network_error_hides_the_url():
    http = Http(posts=[requests.ConnectionError("boom https://www.instagram.com/api/graphql sessionid=SESSIONSECRET")])
    c = client(http)
    c.story_page_items("alice")
    with pytest.raises(InstagramError) as e:
        c.mark_seen("alice", [make_item("11")])
    assert str(e.value) == "mark seen: ConnectionError"


def test_mark_seen_session_reply_reloads_the_session():
    http = Http(posts=[Resp(status=302, headers={"Location": "https://www.instagram.com/accounts/login/"})])
    c = client(http)
    c.story_page_items("alice")
    c._session_loaded = True
    with pytest.raises(SessionError):
        c.mark_seen("alice", [make_item("11")])
    assert c._session_loaded is False


# --- config -----------------------------------------------------------------------------------

def test_mark_seen_defaults_off():
    env = {"IG_USER": "b", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/x"}
    assert load_config(env).mark_seen is False
    assert load_config({**env, "MARK_SEEN": "1"}).mark_seen is True
    assert "mark_seen=True" in repr(load_config({**env, "MARK_SEEN": "1"}))


# --- watcher ----------------------------------------------------------------------------------

def watcher(cfg, store, mark=True):
    ig, n = FakeIG(), FakeNotifier()
    return Watcher(replace(cfg, mark_seen=mark), ig, store, n), ig, n


def seeded(cfg, store, mark=True):
    w, ig, n = watcher(cfg, store, mark)
    w.step()  # seeds alice with nothing
    return w, ig, n


def test_off_by_default_marks_nothing(cfg, store):
    w, ig, n = seeded(cfg, store, mark=False)
    ig.items["alice"] = [make_item("1")]
    w.step()
    assert n.stories and ig.marked == []


def test_marks_posted_items_after_posting(cfg, store):
    w, ig, n = seeded(cfg, store)
    order = []
    story = n.story
    n.story = lambda item, **kw: (order.append("post"), story(item, **kw))[1]
    mark = ig.mark_seen
    ig.mark_seen = lambda t, items: (order.append("mark"), mark(t, items))[1]
    ig.items["alice"] = [make_item("2"), make_item("1", age=__import__("datetime").timedelta(minutes=9))]
    w.step()
    assert ig.marked == [("alice", ["1", "2"])]  # oldest first, one call for the reel
    assert order == ["post", "post", "mark"]
    w.step()  # nothing new: no request
    assert len(ig.marked) == 1


def test_no_request_on_cycles_without_new_items_or_when_seeding(cfg, store):
    w, ig, n = watcher(cfg, store)
    ig.items["alice"] = [make_item("1")]
    w.step()  # seeding
    w.step()
    assert ig.marked == []


def test_filtered_items_are_marked_too(cfg, store):
    cfg = replace(cfg, notify_post_types=frozenset({PostType.JOB_POSTING}))
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]  # no link: misc, filtered out
    w.step()
    assert n.stories == [] and ig.marked == [("alice", ["1"])]


def test_unposted_items_are_not_marked(cfg, store):
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]
    n.fail = True
    w.step()
    assert ig.marked == []
    n.fail = False
    w.step()
    assert ig.marked == [("alice", ["1"])]


def test_mark_failure_is_logged_and_never_retried(cfg, store, caplog):
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]
    ig.mark_error = InstagramError("mark seen: HTTP 500")
    with caplog.at_level(logging.INFO):
        wait = w.step()
    assert w.consecutive_failures == 0 and 300 <= wait <= 600
    assert [i.media_id for i in n.stories] == ["1"]
    assert "marking 1 item(s) seen failed" in caplog.text
    w.step()
    assert len(ig.marked) == 1


def test_unexpected_mark_error_logs_the_type_only(cfg, store, caplog):
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]
    ig.mark_error = ValueError("https://www.instagram.com/secret")
    w.step()
    assert "ValueError" in caplog.text and "secret" not in caplog.text
    assert w.consecutive_failures == 0


def test_mark_session_error_goes_through_the_session_alert(cfg, store):
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]
    ig.mark_error = SessionError("mark seen: redirected to login")
    w.step()
    assert store.is_seen("1") and [i.media_id for i in n.stories] == ["1"]
    assert n.alerts == ["Instagram session needs re-login"]
    assert w.backoff.current is not None


def test_session_error_beats_a_failed_post(cfg, store):
    cfg = replace(cfg, targets=("alice", "bob"))
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]
    ig.items["bob"] = [make_item("2", target="bob")]
    story = n.story

    def flaky(item, **kw):
        if item.target == "bob":
            raise DiscordError("boom")
        return story(item, **kw)

    n.story = flaky
    ig.mark_error = SessionError("mark seen: redirected to login")
    w.step()
    assert ig.marked == [("alice", ["1"])]
    assert "Instagram session needs re-login" in n.alerts


def test_items_handled_before_an_instagram_error_are_not_marked(cfg, store):
    cfg = replace(cfg, targets=("alice", "bob"))
    w, ig, n = seeded(cfg, store)
    ig.items["alice"] = [make_item("1")]
    fetch = ig.fetch_story_items

    def fetch_or_429(target):
        if target == "bob":
            raise InstagramError("429")
        return fetch(target)

    ig.fetch_story_items = fetch_or_429
    w.step()
    assert [i.media_id for i in n.stories] == ["1"] and ig.marked == []
