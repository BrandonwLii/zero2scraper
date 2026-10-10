"""The hybrid classifier with the model client mocked: parsing, fallbacks, budget, the rules.

All inputs are invented. Nothing here touches the network.
"""

import json
from dataclasses import replace
from unittest import mock

import pytest
import requests
from conftest import make_item

from story_watch import llm
from story_watch.hybrid import CdnImages, HybridClassifier
from story_watch.jobs import JobPosting
from story_watch.posting_rules import posting_sponsorship
from story_watch.tag_rules import (ALL, Listing, StoryInputs, context_text, dims_from_model, guard, hybrid_tags,
                                   level_from_title, listing_rules, parse_model_json, role_from_text, rule_company,
                                   rule_sponsorship)
from story_watch.tags import Tags

JOB = "https://job-boards.greenhouse.io/acme/jobs/1"
IMAGE = b"\xff\xd8\xff\xe0fake-jpeg"
FILLER = " The team builds backend services in Python and works with product and design every day."
SPONSOR, NO, UNKNOWN = "sponsor_or_canadian", "no_sponsor", "unknown"


def reply(**over):
    base = {"companies": [], "job_titles": [], "post_type": ["job_posting"], "sponsorship": [UNKNOWN],
            "company": ["other"], "role": ["swe"], "level": ["internship"], "evidence": "invented"}
    return json.dumps({**base, **over})


class FakeClient:
    model = "fake"

    def __init__(self, replies=(), up=True):
        self.replies = list(replies)
        self.up = up
        self.bodies = []
        self.timeouts = []

    def healthy(self):
        return self.up

    def complete(self, body, timeout):
        self.bodies.append(body)
        self.timeouts.append(timeout)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def posting(text="", status="ok", locations=(), title="Software Engineer Intern", company="Acme Robotics", **kw):
    return JobPosting(title=title, company=company, status=status, locations=tuple(locations),
                      description=(text + FILLER * 5) if status == "ok" else "", **kw)


def job_item(p=None, media_id="1", **kw):
    return replace(make_item(media_id), links=(JOB,), job_title=p.title if p else None,
                   company=p.company if p else None, posting=p, **kw)


def classifier(client, ocr="", image=IMAGE, **kw):
    return HybridClassifier(client, lambda item: image, (lambda img: ocr) if ocr is not None else None, **kw)


# -- parsing -------------------------------------------------------------------------------------------


def test_reply_parsing_tolerates_fences_and_prose():
    assert parse_model_json('```json\n{"role": ["ml"]}\n```') == {"role": ["ml"]}
    assert parse_model_json('Here you go: {"role": ["ml"]} thanks') == {"role": ["ml"]}
    for bad in ("", "no json here", "[1, 2]", '{"role": '):
        with pytest.raises(ValueError):
            parse_model_json(bad)


def test_missing_empty_or_invalid_dimensions_become_every_value():
    dims = dims_from_model({"post_type": "event", "role": [], "level": ["bogus"], "company": "quant"})
    assert dims["post_type"] == {"event"} and dims["company"] == {"quant"}
    assert dims["role"] == set(ALL["role"]) and dims["level"] == set(ALL["level"])
    assert dims["sponsorship"] == set(ALL["sponsorship"])


def test_model_decides_post_type_and_role_rules_the_rest():
    p = posting("Located in Austin, TX.", locations=["Austin, TX"])
    tags = classifier(FakeClient([reply(role=["pm"], company=["faang_plus"], sponsorship=[SPONSOR], level=["other"])])).classify(job_item(p))
    assert tags.certain_value("post_type").value == "job_posting"
    assert {v.value for v in tags.role} == {"swe"}  # the listing title settles role over the model
    assert {v.value for v in tags.company} == {"other"}  # named, on neither list
    assert {v.value for v in tags.sponsorship} == {UNKNOWN}  # read, silent, US
    assert {v.value for v in tags.level} == {"internship"}
    assert tags.evidence == "invented"


def test_request_is_10s_and_carries_no_ocr_text():
    client = FakeClient([reply()])
    classifier(client, ocr="OCR-ONLY-WORDS").classify(job_item(posting("x")))
    body = client.bodies[0]
    assert body["temperature"] == 0 and body["max_tokens"] == 320
    assert body["response_format"]["json_schema"]["schema"] is llm.SCHEMA
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert "{faang_list}" not in system and "Marshall Wace" in system and "Meta (Facebook, Instagram" in system
    assert user[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "OCR-ONLY-WORDS" not in user[1]["text"] and "Link sticker: job-boards.greenhouse.io/acme/jobs/1" in user[1]["text"]


# -- fallbacks -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("failure", [llm.LlmTimeout("t"), llm.LlmUnavailable("refused"), llm.LlmError("HTTP 500"),
                                     "not json at all", '{"post_type": '])
def test_any_model_failure_falls_back_to_the_listing_rules(failure, caplog):
    p = posting("We are unable to sponsor visas.", locations=["Seattle, WA"])
    tags = classifier(FakeClient([failure])).classify(job_item(p))
    assert tags.evidence.startswith("fallback: ")
    assert tags.certain_value("post_type").value == "job_posting"  # a link: job posting
    assert {v.value for v in tags.sponsorship} == {NO}  # rules still read the posting
    assert "unable to sponsor" not in caplog.text


def test_timeout_or_refused_skips_the_model_for_the_rest_of_the_cycle():
    client = FakeClient([llm.LlmTimeout("t"), reply()])
    c = classifier(client)
    c.start_cycle()
    c.classify(job_item(posting("x"), "1"))
    c.classify(job_item(posting("x"), "2"))
    assert len(client.bodies) == 1  # the second story didn't queue behind the stuck one
    c.start_cycle()
    assert not c.classify(job_item(posting("x"), "3")).evidence.startswith("fallback")


def test_a_bad_reply_does_not_skip_the_next_story():
    client = FakeClient(["garbage", reply()])
    c = classifier(client)
    c.start_cycle()
    assert c.classify(job_item(posting("x"), "1")).evidence.startswith("fallback")
    assert not c.classify(job_item(posting("x"), "2")).evidence.startswith("fallback")


def test_health_down_uses_the_fallback_without_calling():
    client = FakeClient([], up=False)
    c = classifier(client)
    assert c.start_cycle() is False
    tags = c.classify(job_item(posting("x")))
    assert tags.evidence == "fallback: model unavailable" and client.bodies == []


def test_health_check_that_raises_counts_as_down():
    client = FakeClient()
    client.healthy = mock.Mock(side_effect=RuntimeError("boom"))
    assert classifier(client).start_cycle() is False


def test_no_image_falls_back_and_no_ocr_without_links_is_unsure():
    c = classifier(FakeClient(), image=None)
    assert c.classify(make_item("1")) == Tags(evidence="fallback: no image")
    assert c.classify(job_item(posting("x"))).evidence == "fallback: no image"


def test_ocr_text_feeds_the_fallback():
    c = classifier(FakeClient([], up=False), ocr="Jane Street trading intern interview: final round tips")
    c.start_cycle()
    tags = c.classify(make_item("1"))  # no link: only the image text to go on
    assert {v.value for v in tags.post_type} >= {"process_info"}
    assert {v.value for v in tags.company} == {"quant"}


def test_ocr_failure_is_not_fatal():
    def broken(img):
        raise RuntimeError("onnx")

    tags = HybridClassifier(FakeClient([reply()]), lambda i: IMAGE, broken).classify(job_item(posting("x")))
    assert not tags.evidence.startswith("fallback")


def test_image_download_that_raises_gives_the_fallback():
    def broken(item):
        raise RuntimeError("cdn")

    client = FakeClient([reply()])
    tags = HybridClassifier(client, broken).classify(job_item(posting("x")))
    assert tags.evidence == "fallback: no image" and client.bodies == []


def test_cycle_budget_bounds_a_burst():
    clock = iter(range(0, 10_000, 50)).__next__  # every call "takes" 50 s
    client = FakeClient([reply()] * 20)
    c = classifier(client, cycle_budget=200, clock=lambda: clock())
    c.start_cycle()
    results = [c.classify(job_item(posting("x"), str(i))) for i in range(6)]
    assert len(client.bodies) == 4  # 200 s of budget, 50 s each; the rest get the fallback at once
    assert [r.evidence for r in results[4:]] == ["fallback: cycle model budget spent"] * 2
    assert client.timeouts[0] == 90 and client.timeouts[-1] == 50  # never past the budget


def test_timeout_is_capped_by_the_per_story_setting():
    client = FakeClient([reply()])
    classifier(client, timeout=30).classify(job_item(posting("x")))
    assert client.timeouts == [30]


# -- the uncertainty mapping and the guards ---------------------------------------------------------------


def test_unread_page_sponsorship_unknown_becomes_every_value():
    p = posting(status="failed")
    tags = classifier(FakeClient([reply(sponsorship=[UNKNOWN])])).classify(job_item(p))
    assert tags.is_unsure("sponsorship")


def test_link_keeps_job_posting_unless_the_read_page_is_an_event():
    p = posting("Join our info session.", is_event_page=True, title="Info session")
    tags = classifier(FakeClient([reply(post_type=["event"])])).classify(job_item(p))
    assert {v.value for v in tags.post_type} == {"event"}
    tags = classifier(FakeClient([reply(post_type=["misc"])])).classify(job_item(posting(status="failed")))
    assert {v.value for v in tags.post_type} == {"misc", "job_posting"}


def test_misc_has_no_other_dimensions():
    tags = classifier(FakeClient([reply(post_type=["misc"])])).classify(make_item("1"))
    assert tags.post_type == {"misc"} and all(tags.values(d) is None for d in ("sponsorship", "company", "role", "level"))


def test_student_event_level_is_widened():
    tags = classifier(FakeClient([reply(post_type=["event"], level=["internship"])])).classify(make_item("1"))
    assert {v.value for v in tags.level} == {"internship", "new_grad"}


def test_no_link_process_words_keep_process_info():
    tags = classifier(FakeClient([reply(post_type=["misc"])]), ocr="my final round interview went well").classify(make_item("1"))
    assert {v.value for v in tags.post_type} == {"misc", "process_info"}


# -- rules: sponsorship -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locations, text, expected",
    [
        (["Toronto, ON"], "We do not sponsor visas.", {SPONSOR}),  # in Canada: no sponsorship needed
        (["Austin, TX"], "We will sponsor visas for this role.", {SPONSOR}),
        (["Austin, TX"], "We are unable to sponsor work visas now or in the future.", {NO}),
        (["Austin, TX"], "Must be authorized to work in the United States without sponsorship.", {NO}),
        (["Austin, TX"], "Applicants must be a U.S. person.", {NO}),
        (["Austin, TX"], "Students must be enrolled in a university located in the United States.", {NO}),  # user, #9
        (["Austin, TX"], "US-based applicants only.", {NO}),  # user, #10
        (["Remote, US or Canada"], "US-based applicants only.", {SPONSOR}),  # ...unless open to Canada
        (["Toronto, ON", "Austin, TX"], "US-based applicants only.", {SPONSOR}),
        (["Austin, TX"], "Great benefits and a fun team.", {UNKNOWN}),
    ],
)
def test_posting_sponsorship(locations, text, expected):
    assert posting_sponsorship(locations, text) == expected


def test_tesla_is_a_known_sponsor_unless_the_posting_refuses():
    silent = posting("Austin, TX office.", company="Tesla", locations=["Austin, TX"])
    assert rule_sponsorship(StoryInputs((JOB,), listing=Listing.from_posting(silent)), []) == {SPONSOR}
    refuses = posting("We are unable to sponsor visas.", company="Tesla", locations=["Austin, TX"])
    assert rule_sponsorship(StoryInputs((JOB,), listing=Listing.from_posting(refuses)), []) == {NO}
    assert rule_sponsorship(StoryInputs((JOB,), listing=None), ["Tesla is hiring interns"]) == {SPONSOR}


def test_unread_posting_sponsorship_comes_from_the_image_only():
    unread = StoryInputs((JOB,), listing=Listing("failed"))
    assert rule_sponsorship(unread, ["Remote across North America"]) == {SPONSOR}
    assert rule_sponsorship(unread, ["Austin, TX"]) == set(ALL["sponsorship"])
    assert rule_sponsorship(unread, ["US citizenship required for this role"]) == {NO}


# -- rules: company and level ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texts, expected",
    [
        (["New grad role at Amazon Web Services"], {"faang_plus"}),
        (["Google DeepMind research intern"], {"faang_plus"}),
        (["Citadel Securities"], {"quant"}),
        (["HRT is hiring"], {"quant"}),
        (["hrt emoji text"], set()),  # short aliases need capitals
        (["D.E. Shaw and Meta"], {"quant", "faang_plus"}),
        (["Jump into summer", "Maven of style"], set()),  # common words need context
        (["Jump Trading"], {"quant"}),
        (["Instagram\nAcme Corp careers"], set()),  # the in-app browser's header
    ],
)
def test_company_aliases(texts, expected):
    from story_watch.companies import match_companies

    assert match_companies(texts) == expected


def test_named_unlisted_company_is_other_and_platform_hosts_are_not_the_employer():
    assert rule_company(StoryInputs(sidecar_company="Acme Robotics"), [], set(ALL["company"])) == {"other"}
    assert rule_company(StoryInputs(), ["nothing named"], {"faang_plus", "other"}) == {"faang_plus", "other"}
    teams = StoryInputs(("https://teams.microsoft.com/l/meetup-join/1",), sidecar_company="Microsoft")
    assert rule_company(teams, [], set(ALL["company"])) == set(ALL["company"])


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Software Engineer Intern, Summer 2027", {"internship"}),
        ("8-month Co-op, Software", {"internship"}),
        ("Software Engineer, University Graduate 2027", {"new_grad"}),
        ("Entry-level Data Analyst", {"new_grad"}),
        ("Senior Backend Engineer", {"other"}),
        ("Engineer I, Platform", {"new_grad", "other"}),
        ("Software Engineer", None),
    ],
)
def test_level_from_title(title, expected):
    assert level_from_title(title) == expected


def test_role_from_titles():
    assert role_from_text(["AI Software Engineer Intern"]) == {"ml", "swe"}
    assert role_from_text(["Data Scientist, New Grad"]) == {"other"}
    assert role_from_text(["Associate Product Manager"]) == {"pm"}
    assert role_from_text([""]) == set(ALL["role"])


def test_fallback_without_ocr_or_link_is_unsure():
    assert listing_rules(StoryInputs(), None) == Tags.unsure()


def test_guard_known_sponsor_on_the_image():
    dims = {**{k: set(v) for k, v in ALL.items()}, "sponsorship": {UNKNOWN}}
    assert guard(dims, StoryInputs((JOB,), listing=Listing("failed")), ["Tesla"])["sponsorship"] == {SPONSOR}


def test_context_text_says_when_the_page_was_not_read():
    text = context_text(StoryInputs((JOB,), ("acme",), "SWE Intern", listing=Listing("closed")))
    assert "Linked page: closed" in text and "Page title from metadata: SWE Intern" in text and "@acme" in text


def test_hybrid_tags_widen_unread_unknown():
    tags = hybrid_tags(json.loads(reply()), StoryInputs((JOB,), listing=Listing("failed")), None)
    assert tags.is_unsure("sponsorship")


# -- the HTTP client ---------------------------------------------------------------------------------------------


def _session(post=None, get=None):
    s = mock.Mock()
    s.post.side_effect = post
    s.get.side_effect = get
    return s


def _resp(status=200, payload=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    return r


@pytest.mark.parametrize(
    "outcome, error",
    [
        (requests.ReadTimeout("read timed out"), llm.LlmTimeout),
        (requests.ConnectionError("refused"), llm.LlmUnavailable),
        (_resp(503), llm.LlmUnavailable),
        (_resp(500), llm.LlmError),
        (_resp(200, {"choices": []}), llm.LlmError),
        (_resp(200, {"choices": [{"message": {"content": None}}]}), llm.LlmError),
    ],
)
def test_client_errors(outcome, error):
    def post(*a, **kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = llm.LlmClient("http://127.0.0.1:8090", session=_session(post=post))
    with pytest.raises(error) as e:
        client.complete({"messages": []}, 90)
    assert "127.0.0.1" not in str(e.value)  # fixed messages only


def test_client_reply_and_timeout():
    seen = {}

    def post(url, json=None, timeout=None):
        seen.update(url=url, timeout=timeout)
        return _resp(200, {"choices": [{"message": {"content": "{}"}}]})

    client = llm.LlmClient("http://127.0.0.1:8090/", session=_session(post=post))
    assert client.complete({}, 90) == "{}"
    assert seen == {"url": "http://127.0.0.1:8090/v1/chat/completions", "timeout": (3.0, 90)}


def test_client_health():
    ok = llm.LlmClient("http://127.0.0.1:8090", session=_session(get=lambda *a, **kw: _resp(200)))
    loading = llm.LlmClient("http://127.0.0.1:8090", session=_session(get=lambda *a, **kw: _resp(503)))
    def refused(*a, **kw):
        raise requests.ConnectionError("refused")
    down = llm.LlmClient("http://127.0.0.1:8090", session=_session(get=refused))
    assert ok.healthy() and not loading.healthy() and not down.healthy()


# -- the still image ---------------------------------------------------------------------------------------------


class _Img:
    def __init__(self, status=200, body=IMAGE):
        self.status_code, self._body = status, body

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


def test_cdn_image_is_pinned_and_guarded():
    calls = []

    def transport(method, url, ip, headers, timeout, body=None):
        calls.append((url, ip, headers))
        return _Img()

    public = lambda h, p, proto=0: [(2, 1, 6, "", ("93.184.215.14", p))]  # noqa: E731
    item = replace(make_item("1"), thumbnail_url="https://cdn.example/1.jpg")
    assert CdnImages(resolve=public, transport=transport)(item) == IMAGE
    assert calls[0][1] == "93.184.215.14" and "Cookie" not in calls[0][2]
    private = lambda h, p, proto=0: [(2, 1, 6, "", ("192.168.1.5", p))]  # noqa: E731
    assert CdnImages(resolve=private, transport=transport)(item) is None
    assert CdnImages(resolve=public, transport=transport)(replace(item, thumbnail_url="http://cdn.example/1.jpg")) is None
    assert CdnImages(resolve=public, transport=lambda *a, **kw: _Img(status=403))(item) is None


def test_cdn_image_size_cap(monkeypatch):
    monkeypatch.setattr("story_watch.hybrid.MAX_IMAGE_BYTES", 4)
    public = lambda h, p, proto=0: [(2, 1, 6, "", ("93.184.215.14", p))]  # noqa: E731
    item = replace(make_item("1"), thumbnail_url="https://cdn.example/1.jpg")
    assert CdnImages(resolve=public, transport=lambda *a, **kw: _Img())(item) is None


# -- the registry ----------------------------------------------------------------------------------------------------


def test_hybrid_is_registered_and_built_from_config(cfg):
    from story_watch.classify import build_classifier

    c = build_classifier("hybrid", replace(cfg, classifier="hybrid", llm_timeout=45, tagger_ocr=False))
    assert isinstance(c, HybridClassifier) and c.timeout == 45 and c.ocr is None
    assert c.client.url == "http://127.0.0.1:8090"


def test_rules_crashing_on_a_reply_still_give_the_fallback(monkeypatch):
    monkeypatch.setattr("story_watch.hybrid.hybrid_tags", mock.Mock(side_effect=KeyError("x")))
    tags = classifier(FakeClient([reply()])).classify(job_item(posting("x")))
    assert tags.evidence == "fallback: KeyError"


@pytest.mark.parametrize("unit", ["1" + " " * 50_000, "\n", " \n", "1 ", "instagram\n", "remote ", "not "])
def test_rules_stay_linear_on_pathological_text(unit):
    import time

    from story_watch.posting_rules import level

    text = (unit * (50_000 // len(unit) + 1))[:50_000]  # MAX_DESCRIPTION-sized hostile posting and OCR text
    lst = Listing(status="ok", title="Software Engineer", locations=("Seattle, WA",), description="1" + text)
    inp = StoryInputs(links=("https://careers.example.com/jobs/1",), listing=lst)
    start = time.monotonic()
    hybrid_tags({"post_type": ["job_posting"], "companies": [text[:300]]}, inp, text)
    listing_rules(inp, text), listing_rules(StoryInputs(), text), level("", "1" + text)
    assert time.monotonic() - start < 5  # the quadratic patterns took ~40 s and ~9 s


def test_years_and_ui_noise_rewrites_keep_their_matches():
    from story_watch.companies import match_companies
    from story_watch.posting_rules import min_years

    assert min_years("Requires 3 - 5 years of experience, or 2+ years professional work, 1 to 2 years industry.") == 1
    assert min_years("10 +years of experience") == 10 and min_years("5 years ago we had no experience") == 5
    assert match_companies(["Instagram\n  instagram  \nTwo Sigma"]) == {"quant"}
    assert match_companies(["  Instagram \n"]) == set() and match_companies(["Meta on Instagram"]) == {"faang_plus"}
