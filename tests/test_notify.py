import pytest
import requests

from conftest import make_item

from story_watch.notify import DiscordError, Notifier

URL = "https://discord.com/api/webhooks/123/secret-token"


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_429_respects_retry_after():
    sleeps = []
    http = FakeSession([FakeResp(429, {"retry_after": 2.5}), FakeResp(204)])
    Notifier(URL, session=http, sleep=sleeps.append).story(make_item("1"))
    assert sleeps == [2.5]
    assert len(http.calls) == 2


def test_5xx_retries_then_succeeds():
    sleeps = []
    http = FakeSession([FakeResp(502), FakeResp(503), FakeResp(200, {})])
    Notifier(URL, session=http, sleep=sleeps.append).alert("t", "d")
    assert len(sleeps) == 2


def test_4xx_raises_without_retry():
    http = FakeSession([FakeResp(404)])
    with pytest.raises(DiscordError):
        Notifier(URL, session=http, sleep=lambda s: None).alert("t", "d")
    assert len(http.calls) == 1


def test_network_error_does_not_leak_url():
    err = requests.ConnectionError(f"Max retries exceeded with url: {URL}")
    http = FakeSession([err, err])
    with pytest.raises(DiscordError) as exc:
        Notifier(URL, session=http, max_attempts=2, sleep=lambda s: None).alert("t", "d")
    assert "secret-token" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_story_embed_shape():
    http = FakeSession([FakeResp(204)])
    Notifier(URL, session=http).story(make_item("1", target="zero2sudo"))
    embed = http.calls[0]["json"]["embeds"][0]
    assert embed["title"] == "New story from @zero2sudo"
    assert embed["url"] == "https://www.instagram.com/stories/zero2sudo/"
    assert embed["image"]["url"].endswith("1.jpg")
