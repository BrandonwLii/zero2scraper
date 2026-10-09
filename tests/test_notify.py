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
    assert embed["title"] == "Sigma Computing: Software Engineering Intern (Summer 2027)"
    assert embed["url"] == JOB
    assert embed["author"] == {"name": "New story from @zero2sudo"}
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
    assert [f["name"] for f in embed["fields"]] == ["Posted"]
    assert "footer" not in embed


def test_job_title_without_company_or_already_prefixed():
    assert _embed(_story(links=(JOB,), job_title="SWE Intern"))["title"] == "SWE Intern"
    prefixed = _story(links=(JOB,), job_title="Shield AI - Engineer I", company="Shield AI")
    assert _embed(prefixed)["title"] == "Shield AI - Engineer I"


def test_company_without_job_title():
    assert _embed(_story(links=(JOB,), company="Otter.ai"))["title"] == "Otter.ai: job posting"


def _payload(item, **kw):
    http = FakeSession([FakeResp(204)])
    Notifier(URL, session=http).story(item, **kw)
    return http.calls[0]["json"]


def uid(n):
    return str(100000000000000000 + n)


def test_users_are_pinged_and_only_those_users_allowed():
    from story_watch.tags import PostType, Tags

    item = _story(links=(JOB,), job_title="SWE Intern", company="Sigma Computing")
    p = _payload(item, tags=Tags(post_type=[PostType.JOB_POSTING]), user_ids=(uid(1), uid(2)))
    assert p["content"] == f"Sigma Computing: SWE Intern <@{uid(1)}> <@{uid(2)}>"
    assert p["allowed_mentions"] == {"parse": [], "users": [uid(1), uid(2)]}
    assert p["embeds"][0]["footer"] == {"text": "Job posting · Sponsorship: ? · Company: ? · Role: ? · Level: ?"}


def test_no_users_means_no_content_and_no_mentions():
    p = _payload(_story())
    assert "content" not in p
    assert p["allowed_mentions"] == {"parse": []}


def test_bad_and_duplicate_ids_never_reach_content():
    p = _payload(_story(), user_ids=(uid(1), uid(1), "everyone", "&123456789012345678", "12", uid(2)))
    assert p["content"].endswith(f"<@{uid(1)}> <@{uid(2)}>")
    assert p["allowed_mentions"] == {"parse": [], "users": [uid(1), uid(2)]}
    assert "everyone" not in p["content"] and "&" not in p["content"]


def _post_all(user_ids, responses=None):
    http = FakeSession(responses or [FakeResp(204)] * 10)
    Notifier(URL, session=http, sleep=lambda s: None).story(_story(), user_ids=user_ids)
    return [c["json"] for c in http.calls]


def test_a_few_users_make_one_message():
    assert len(_post_all([uid(i) for i in range(5)])) == 1


def test_more_than_100_ids_overflow_into_follow_ups():
    ids = [uid(i) for i in range(250)]
    posts = _post_all(ids)
    seen = []
    for post in posts:
        users = post["allowed_mentions"]["users"]
        assert post["allowed_mentions"]["parse"] == []
        assert len(users) <= 100 and len(post["content"]) <= 2000
        assert post["content"].count("<@") == len(users)
        seen += users
    assert seen == ids  # every id exactly once, in order
    assert "embeds" in posts[0] and all("embeds" not in p for p in posts[1:])
    assert all(p["content"].startswith("<@") for p in posts[1:])  # follow-ups are mention-only


def test_content_over_2000_characters_overflows_even_under_100_ids():
    ids = [str(10**20 + i) for i in range(95)]  # 21 digits: 25 characters each with its space
    posts = _post_all(ids)
    assert len(posts) == 2
    assert all(len(p["content"]) <= 2000 and len(p["allowed_mentions"]["users"]) <= 100 for p in posts)
    assert sum(len(p["allowed_mentions"]["users"]) for p in posts) == 95


def test_long_title_still_leaves_room_for_mentions():
    item = _story(links=(JOB,), job_title="x" * 1000, company="Sigma")
    http = FakeSession([FakeResp(204)] * 5)
    Notifier(URL, session=http).story(item, user_ids=[uid(i) for i in range(90)])
    assert all(len(c["json"]["content"]) <= 2000 for c in http.calls)
    assert sum(len(c["json"]["allowed_mentions"]["users"]) for c in http.calls) == 90


def test_failed_follow_up_is_logged_and_dropped(caplog):
    http = FakeSession([FakeResp(204), FakeResp(400), FakeResp(204)])
    ids = [uid(i) for i in range(250)]
    with caplog.at_level("WARNING"):
        Notifier(URL, session=http, sleep=lambda s: None).story(_story(), user_ids=ids)  # no raise
    assert len(http.calls) == 3  # the third message was still tried
    assert "secret-token" not in caplog.text


def test_failed_main_post_raises_and_sends_no_follow_ups():
    http = FakeSession([FakeResp(400)])
    with pytest.raises(DiscordError):
        Notifier(URL, session=http).story(_story(), user_ids=[uid(i) for i in range(250)])
    assert len(http.calls) == 1  # nobody was pinged


def test_tags_text():
    from story_watch.notify import tags_text
    from story_watch.tags import Company, Level, PostType, Role, Tags

    assert tags_text(Tags(post_type=[PostType.MISC])) == "Misc"
    t = Tags(post_type=[PostType.JOB_POSTING], company=[Company.QUANT], role=[Role.SWE, Role.PM],
             level=[Level.INTERNSHIP])
    assert tags_text(t) == "Job posting · Sponsorship: ? · Company: Quant · Role: SWE/PM · Level: Internship"
    assert tags_text(Tags.unsure()) == "? · Sponsorship: ? · Company: ? · Role: ? · Level: ?"
