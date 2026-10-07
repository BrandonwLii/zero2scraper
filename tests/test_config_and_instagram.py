from datetime import datetime
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


def test_fetch_maps_items_and_caches_userid(fake_profile):
    raw = SimpleNamespace(mediaid=99, date_utc=datetime(2026, 1, 1, 12), is_video=True, url="https://x/t.jpg")
    loader = FakeLoader(stories=[SimpleNamespace(get_items=lambda: [raw])])
    client = InstagramClient("b", loader=loader)
    items = client.fetch_story_items("alice")
    client.fetch_story_items("alice")
    assert items[0].media_id == "99" and items[0].is_video
    assert items[0].taken_at.tzinfo is not None
    assert fake_profile == ["alice"]
    assert loader.session_loads == 1


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
