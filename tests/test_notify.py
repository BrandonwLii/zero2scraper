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


def _story(**kw):
    from dataclasses import replace

    return replace(make_item("1", target="zero2sudo"), **kw)


def _embed(item):
    http = FakeSession([FakeResp(204)])
    Notifier(URL, session=http).story(item)
    return http.calls[0]["json"]["embeds"][0]


JOB = "https://job-boards.greenhouse.io/sigmacomputing/jobs/8001295003"


def test_job_story_uses_job_title():
    embed = _embed(_story(links=(JOB,), job_title="Software Engineering Intern (Summer 2027)",
                          company="Sigma Computing", mentions=("claudeai",)))
    assert embed["title"] == "Software Engineering Intern (Summer 2027)"
    assert embed["url"] == JOB
    assert embed["author"] == {"name": "Sigma Computing · @zero2sudo"}
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Link"] == f"[job-boards.greenhouse.io/sigmacomputing]({JOB})"
    assert fields["Mentions"] == "@claudeai"
    assert fields["Story"] == "[Open on Instagram](https://www.instagram.com/stories/zero2sudo/)"


def test_job_story_without_title_names_the_site():
    embed = _embed(_story(links=(JOB,)))
    assert embed["title"] == "@zero2sudo: job-boards.greenhouse.io/sigmacomputing"
    assert embed["url"] == JOB
    assert embed["author"] == {"name": "New story from @zero2sudo"}


def test_non_job_link_keeps_generic_title():
    embed = _embed(_story(links=("https://youtube.com/@zero2sudo",)))
    assert embed["title"] == "New story from @zero2sudo"
    assert embed["url"] == "https://www.instagram.com/stories/zero2sudo/"
    assert "author" not in embed
    assert {f["name"]: f["value"] for f in embed["fields"]}["Link"] == "[youtube.com](https://youtube.com/@zero2sudo)"


def test_plain_story_has_no_link_fields():
    embed = _embed(_story())
    assert [f["name"] for f in embed["fields"]] == ["Type", "Posted"]
