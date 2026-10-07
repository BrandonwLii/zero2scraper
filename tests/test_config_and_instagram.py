from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from instaloader import exceptions as ie

from story_watch import instagram
from story_watch.config import ConfigError, load_config
from story_watch.instagram import InstagramClient, InstagramError, SessionError

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


def test_config_rejects_bad_webhook():
    with pytest.raises(ConfigError):
        load_config({"IG_USER": "b", "DISCORD_WEBHOOK": "http://example.com"})


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
