import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from instaloader import exceptions as ie

from story_watch import instagram
from story_watch.config import ConfigError, load_config
from story_watch.instagram import InstagramClient, InstagramError, SessionError, links_from_item, unwrap_link

HOOK = "https://discord.com/api/webhooks/1/secret"


def test_config_parses_and_redacts():
    cfg = load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK, "TARGETS": "@Alice, bob ,"})
    assert cfg.targets == ("alice", "bob")
    assert cfg.heartbeat_hour is None
    assert "secret" not in repr(cfg)


def test_config_parses_target_userids():
    cfg = load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK, "TARGETS": "Zero2Sudo:50350974961, bob"})
    assert cfg.targets == ("zero2sudo", "bob")
    assert cfg.target_ids == {"zero2sudo": 50350974961}


def test_config_rejects_non_numeric_userid():
    with pytest.raises(ConfigError):
        load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK, "TARGETS": "alice:abc"})


def test_config_filter_settings():
    from story_watch.tags import PostType

    cfg = load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK})
    assert cfg.notify_post_types == frozenset(PostType) and cfg.classifier == "rules"
    cfg = load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK, "NOTIFY_MISC": "false"})
    assert cfg.notify_post_types == {PostType.JOB_POSTING, PostType.PROCESS_INFO, PostType.EVENT}


def test_old_interview_info_name_still_works_with_a_warning(caplog):
    from story_watch.tags import PostType

    env = {"IG_USER": "b", "DISCORD_WEBHOOK": HOOK, "NOTIFY_INTERVIEW_INFO": "false"}
    with caplog.at_level("WARNING"):
        cfg = load_config(env)
    assert PostType.PROCESS_INFO not in cfg.notify_post_types
    assert "NOTIFY_INTERVIEW_INFO is deprecated" in caplog.text
    # the new name wins when both are set
    cfg = load_config({**env, "NOTIFY_PROCESS_INFO": "true"})
    assert PostType.PROCESS_INFO in cfg.notify_post_types


def test_removed_role_settings_warn_once_by_name_and_are_ignored(caplog):
    with caplog.at_level("WARNING"):
        cfg = load_config({
            "IG_USER": "b", "DISCORD_WEBHOOK": HOOK, "PING_ROLES": "true",
            "ROLE_JOB_POSTING": "123456789012345678", "ROLE_INTERVIEW_INFO": "223456789012345678",
            "TEST_ROLE_MISC": "323456789012345678", "ROLE_EVENT": "",  # blank = not set
        })
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1
    for name in ("PING_ROLES", "ROLE_JOB_POSTING", "ROLE_INTERVIEW_INFO", "TEST_ROLE_MISC"):
        assert name in warned[0]
    assert "ROLE_EVENT" not in warned[0]
    assert "123456789012345678" not in caplog.text and "223456789012345678" not in caplog.text
    assert not hasattr(cfg, "ping_roles") and not hasattr(cfg, "role_ids")


def test_no_warning_without_removed_settings(caplog):
    with caplog.at_level("WARNING"):
        load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK})
    assert not caplog.records


@pytest.mark.parametrize("env", [{"CLASSIFIER": "gpt"}, {"NOTIFY_MISC": "maybe"}])
def test_config_rejects_bad_settings(env):
    with pytest.raises(ConfigError):
        load_config({"IG_USER": "b", "DISCORD_WEBHOOK": HOOK, **env})


def test_config_rejects_bad_webhook():
    with pytest.raises(ConfigError):
        load_config({"IG_USER": "b", "DISCORD_WEBHOOK": "http://example.com"})


def test_leftover_numbered_vars_warn_by_name_and_are_ignored(caplog):
    with caplog.at_level("WARNING"):
        cfg = load_config({
            "IG_USER": "b", "DISCORD_WEBHOOK": HOOK,
            "DISCORD_WEBHOOK_2": "https://discord.com/api/webhooks/2/hidden",
            "ROLE_JOB_POSTING_2": "222222222222222222",
            "ROLE_MISC_3": "",  # blank = not set, no warning
        })
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 2
    assert any(m.startswith("DISCORD_WEBHOOK_2 ") for m in warned)
    assert any(m.startswith("ROLE_JOB_POSTING_2 ") for m in warned)
    assert "hidden" not in caplog.text and "222222222222222222" not in caplog.text
    assert cfg.discord_webhook == HOOK


class FakeLoader:
    def __init__(self, stories=None, error=None):
        self.context = object()
        self.stories = stories or []
        self.error = error
        self.session_loads = 0

    def load_session_from_file(self, user, path):
        self.session_loads += 1

    def get_stories(self, userids):
        if self.error:
            raise self.error
        return self.stories


@pytest.fixture(autouse=True)
def fake_profile(monkeypatch):
    calls = []

    def from_username(ctx, name):
        calls.append(name)
        return SimpleNamespace(userid=42)

    monkeypatch.setattr(instagram.instaloader.Profile, "from_username", staticmethod(from_username))
    return calls


# Trimmed from a real reels_media capture (2026-10-07); CDN URLs replaced.
NODE = {
    "__typename": "GraphStoryVideo",
    "id": "4002917559397704439",
    "taken_at_timestamp": 1791404977,
    "expiring_at_timestamp": 1791491377,
    "is_video": True,
    "display_url": "https://cdn.example/1080.jpg",
    "display_resources": [
        {"src": "https://cdn.example/640.jpg", "config_width": 640, "config_height": 1136},
        {"src": "https://cdn.example/1080.jpg", "config_width": 1080, "config_height": 1920},
    ],
    "story_cta_url": None,
    "tappable_objects": [],
}


def story(*nodes):
    def get_items():  # pragma: no cover - must not be called
        raise AssertionError("get_items hits the iPhone endpoint")

    return SimpleNamespace(_node={"items": list(nodes)}, get_items=get_items)


def test_fetch_maps_items_and_caches_userid(fake_profile):
    loader = FakeLoader(stories=[story(NODE)])
    client = InstagramClient("b", loader=loader)
    items = client.fetch_story_items("alice")
    client.fetch_story_items("alice")
    assert items[0].media_id == "4002917559397704439" and items[0].is_video
    assert items[0].taken_at == datetime(2026, 10, 7, 20, 29, 37, tzinfo=timezone.utc)
    assert items[0].thumbnail_url == "https://cdn.example/1080.jpg"
    assert fake_profile == ["alice"]
    assert loader.session_loads == 1


def test_known_userid_skips_profile_lookup(fake_profile):
    client = InstagramClient("b", loader=FakeLoader(stories=[story(NODE)]), userids={"alice": 7})
    assert len(client.fetch_story_items("alice")) == 1
    assert fake_profile == []


def test_malformed_story_node_is_instagram_error():
    client = InstagramClient("b", loader=FakeLoader(stories=[story({"id": "1"})]), userids={"alice": 7})
    with pytest.raises(InstagramError):
        client.fetch_story_items("alice")


def test_login_required_becomes_session_error_and_reloads_session():
    loader = FakeLoader(error=ie.LoginRequiredException("login required"))
    client = InstagramClient("b", loader=loader)
    for _ in range(2):
        with pytest.raises(SessionError):
            client.fetch_story_items("alice")
    assert loader.session_loads == 2


def test_checkpoint_message_becomes_session_error():
    client = InstagramClient("b", loader=FakeLoader(error=ie.ConnectionException("checkpoint_required")))
    with pytest.raises(SessionError):
        client.fetch_story_items("alice")


def test_rate_limit_is_plain_instagram_error():
    client = InstagramClient("b", loader=FakeLoader(error=ie.TooManyRequestsException("429")))
    with pytest.raises(InstagramError) as exc:
        client.fetch_story_items("alice")
    assert not isinstance(exc.value, SessionError)


# story_link_stickers excerpt from the web client (2026-10-07), as served (JSON-escaped).
LINK_ITEM = json.loads(
    r'''{"story_bloks_stickers":null,"story_link_stickers":[{"x":0.5,"y":0.59638315,"width":0.99209875,'''
    r'''"height":0.0765625,"rotation":0,"story_link":{"url":"https:\/\/l.instagram.com\/?u=https\u00253A\u00252F'''
    r'''\u00252Fjob-boards.greenhouse.io\u00252Fsigmacomputing\u00252Fjobs\u00252F8001295003\u00253Ffbclid'''
    r'''\u00253DPAZXh0bgNhZW0&e=AUCPoTz0yjouTTfy"}}]}'''
)


def test_links_from_item_unwraps_redirect_and_strips_tracking():
    assert links_from_item(LINK_ITEM) == ("https://job-boards.greenhouse.io/sigmacomputing/jobs/8001295003",)


def test_links_from_item_handles_missing_stickers():
    assert links_from_item({"story_link_stickers": None}) == ()
    assert links_from_item({}) == ()


def test_unwrap_link_keeps_real_query_and_plain_urls():
    assert unwrap_link("https://example.com/a?id=3&utm_source=ig&fbclid=x") == "https://example.com/a?id=3"
    assert unwrap_link("https://l.instagram.com/?u=javascript%3Aalert(1)&e=x").startswith("https://l.instagram.com/")


# Shape of the /stories/<user>/ page: items nested deep inside script JSON.
PAGE_ITEMS = [
    {"pk": "4002917559397704439", "taken_at": 1791404977, "media_type": 2,
     "story_link_stickers": [{"story_link": {"url": "https://l.instagram.com/?u=https%3A%2F%2Fjobs.ashbyhq.com%2Facme%2F1&e=x"}}],
     "story_bloks_stickers": [{"bloks_sticker": {"sticker_data": {"ig_mention": {"full_name": "Claude", "username": "claudeai"}}}}]},
    {"pk": "4002905480954986192", "taken_at": 1791403539, "media_type": 1,
     "story_link_stickers": None, "story_bloks_stickers": None},
]
PAGE = (
    '<html><script type="application/json" data-sjs>{"x":1}</script>'
    '<script type="application/json" data-content-len="9" data-sjs>'
    + json.dumps({"require": [["S", "h", None, [{"__bbox": {"result": {"data": {
        "xdt_api__v1__feed__reels_media": {"reels_media": [{"items": PAGE_ITEMS}]}}}}}]]]}).replace("/", "\\/")
    + "</script><script>not json</script></html>"
)


def test_items_from_story_page():
    from story_watch.instagram import items_from_story_page, mentions_from_item

    items = items_from_story_page(PAGE)
    assert sorted(items) == ["4002905480954986192", "4002917559397704439"]
    assert links_from_item(items["4002917559397704439"]) == ("https://jobs.ashbyhq.com/acme/1",)
    assert mentions_from_item(items["4002917559397704439"]) == ("claudeai",)
    assert mentions_from_item(items["4002905480954986192"]) == ()


class PageResp:
    def __init__(self, text="", status=200, url="https://www.instagram.com/stories/alice/?r=1"):
        self.text, self.status_code, self.url = text, status, url


class PageSession:
    def __init__(self, resp):
        self.resp = resp
        self.headers = {}
        self.cookies = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_with_extras_matches_items_by_id():
    from story_watch.instagram import _item_from_node

    page = PageSession(PageResp(PAGE))
    client = InstagramClient("b", loader=FakeLoader(), userids={"alice": 7}, page_session=page)
    linked = _item_from_node(dict(NODE, id="4002917559397704439"), "alice")
    unknown = _item_from_node(dict(NODE, id="1"), "alice")
    out = client.with_extras("alice", [linked, unknown])
    assert out[0].links == ("https://jobs.ashbyhq.com/acme/1",) and out[0].mentions == ("claudeai",)
    assert out[1] == unknown
    assert page.calls == ["https://www.instagram.com/stories/alice/"]


@pytest.mark.parametrize(
    "resp",
    [
        PageResp(url="https://www.instagram.com/accounts/login/?next=/stories/alice/"),
        PageResp(status=429),
        __import__("requests").ConnectionError("boom https://www.instagram.com/stories/alice/"),
    ],
)
def test_with_extras_failures_are_instagram_errors(resp):
    client = InstagramClient("b", loader=FakeLoader(), userids={"alice": 7}, page_session=PageSession(resp))
    with pytest.raises(InstagramError) as exc:
        client.with_extras("alice", [])
    assert "boom" not in str(exc.value)
