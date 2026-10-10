import html
import json

import pytest

from story_watch.jobs import (Blocked, JobPosting, JobTitles, PinnedAdapter, SafeFetcher, check_public, company_from_html,
                              is_job_link, pinned_url, title_from_html, title_from_slug)


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


@pytest.mark.parametrize(
    "page, expected",
    [
        # Workday/Lever/Ashby style: JSON-LD wins over a branded og:title
        (ld({"@type": "JobPosting", "title": "Embedded Software Engineer Co-op"})
         + '<meta property="og:title" content="Embedded Software Engineer Co-op | Bose">', "Embedded Software Engineer Co-op"),
        (ld({"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "SWE Intern"}]}), "SWE Intern"),
        # Greenhouse: og:title is the bare job name
        ('<meta property="og:title" content="Software Engineering Intern (Summer 2027)">'
         "<title>Job Application for Software Engineering Intern (Summer 2027) at Sigma Computing</title>",
         "Software Engineering Intern (Summer 2027)"),
        ("<title>Job Application for Security Engineer Intern at Figure</title>", "Security Engineer Intern"),
        ('<meta content="A &amp; B Intern" property="og:title">', "A & B Intern"),
        # careers landing pages aren't job names
        ('<meta property="og:title" content="Otter.ai Careers - Shape the Future">', None),
        ("<title>S&amp;C Minimal Career Site</title>", None),
        ("<html></html>", None),
    ],
)
def test_title_from_html(page, expected):
    assert title_from_html(page) == expected


def test_title_from_slug():
    icims = "https://x.icims.com/jobs/19550/intern,-applied-ai-&-full-stack-development/job?ref=z"
    assert title_from_slug(icims) == "Intern, applied ai & full stack development"
    assert title_from_slug("https://jobs.lever.co/acme/5c1d2e3f-0000-4000-8000-00000000abcd") is None
    assert title_from_slug("https://job-boards.greenhouse.io/cloudflare/jobs/8245211") is None


@pytest.mark.parametrize(
    "url, job",
    [
        ("https://job-boards.greenhouse.io/sigmacomputing/jobs/8001295003", True),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/X_JR1", True),
        ("https://careers.ibm.com/en_US/careers/JobDetail?jobId=135839", True),
        ("https://www.amazon.jobs/en/jobs/10571374/sde-intern", True),
        ("https://otter.ai/job-detail?gh_jid=8016078003", True),
        ("https://youtube.com/@zero2sudo", False),
        ("https://example.com/blog/my-jobsite-review", False),
    ],
)
def test_is_job_link(url, job):
    assert is_job_link(url) is job


class Resp:
    def __init__(self, status=200, body="", location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None
        self.encoding = "utf-8"
        self._body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.closed = False

    def iter_content(self, n):
        yield self._body

    def close(self):
        self.closed = True


class FakeTransport:
    """Stands in for the pinned connection: records (method, url, ip, Host header); unknown URLs 404."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, ip, headers, timeout, body=None):
        self.calls.append((method, url, ip, headers.get("Host")))
        return self.routes.get(url) or Resp(status=404)

    @property
    def urls(self):
        return [c[1] for c in self.calls]


def resolver(ips):
    def resolve(host, port, proto=0):
        return [(2, 1, 6, "", (ips.get(host, "93.184.215.14"), port))]

    return resolve


GH = "https://job-boards.greenhouse.io/acme/jobs/1"
GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"
OG = '<meta property="og:title" content="SWE Intern">'


def test_lookup_fetches_and_caches_until_the_next_cycle():
    http = FakeTransport({GH_API: Resp(status=500), GH: Resp(body=OG)})
    jt = JobTitles(resolve=resolver({}), transport=http)
    got = jt.lookup(GH)
    assert (got.title, got.company, got.status) == ("SWE Intern", None, "failed")  # no posting text
    assert jt.lookup(GH).title == "SWE Intern"
    assert http.urls == [GH_API, GH]
    jt.start_cycle()
    jt.lookup(GH)
    assert http.urls == [GH_API, GH] * 2


def test_lookup_refuses_private_addresses_and_http():
    http = FakeTransport({})
    jt = JobTitles(resolve=resolver({"job-boards.greenhouse.io": "10.0.0.5", "boards-api.greenhouse.io": "10.0.0.5"}),
                   transport=http)
    assert jt.lookup(GH) == JobPosting()
    assert jt.lookup("http://example.com/jobs/1") == JobPosting()
    assert http.calls == []


def test_redirect_to_private_address_is_not_followed():
    http = FakeTransport({GH: Resp(status=302, location="https://router.lan/jobs/1")})
    jt = JobTitles(resolve=resolver({"router.lan": "192.168.1.1"}), transport=http)
    assert jt.lookup(GH) == JobPosting()
    assert http.urls == [GH_API, GH]


def test_redirect_is_followed():
    final = "https://boards.example/jobs/1"
    http = FakeTransport({GH: Resp(status=301, location=final), final: Resp(body=OG)})
    assert JobTitles(resolve=resolver({}), transport=http).lookup(GH).title == "SWE Intern"


def test_error_page_falls_back_to_slug():
    url = "https://x.icims.com/jobs/1/intern,-applied-ai-development/job"
    http = FakeTransport({url + "?in_iframe=1": Resp(status=403)})
    got = JobTitles(resolve=resolver({}), transport=http).lookup(url)
    assert (got.title, got.company, got.status) == ("Intern, applied ai development", None, "failed")
    assert http.urls == [url + "?in_iframe=1"]  # iCIMS: the iframe document, not the empty frame


# -- DNS pinning (the rebinding gap #9 found) -----------------------------------------------------


def test_connects_to_the_checked_address_with_the_hostname_for_tls():
    http = FakeTransport({"https://boards.example/jobs/1": Resp(body=OG)})
    f = SafeFetcher(resolve=resolver({"boards.example": "93.184.215.20"}), transport=http)
    f.get("https://boards.example/jobs/1")
    assert http.calls == [("GET", "https://boards.example/jobs/1", "93.184.215.20", None)]


def test_a_second_dns_answer_cannot_redirect_the_connection():
    """Rebinding: the name resolves once per hop, and that checked answer is the one connected to."""
    answers = iter(["93.184.215.20", "127.0.0.1"])
    seen = []

    def rebinding(host, port, proto=0):
        ip = next(answers)
        seen.append(ip)
        return [(2, 1, 6, "", (ip, port))]

    http = FakeTransport({"https://boards.example/jobs/1": Resp(body=OG)})
    SafeFetcher(resolve=rebinding, transport=http).get("https://boards.example/jobs/1")
    assert seen == ["93.184.215.20"] and http.calls[0][2] == "93.184.215.20"


def test_every_address_must_be_public():
    def mixed(host, port, proto=0):
        return [(2, 1, 6, "", ("93.184.215.20", port)), (10, 1, 6, "", ("fd00::1", port, 0, 0))]

    with pytest.raises(Blocked):
        check_public("https://boards.example/", mixed)
    with pytest.raises(Blocked):
        check_public("https://boards.example/", lambda *a, **kw: [])


def test_redirect_hops_are_each_checked_and_pinned():
    a, b = "https://a.example/jobs/1", "https://b.example/jobs/1"
    http = FakeTransport({a: Resp(status=302, location=b), b: Resp(body=OG)})
    SafeFetcher(resolve=resolver({"a.example": "93.184.215.1", "b.example": "93.184.215.2"}), transport=http).get(a)
    assert [(c[1], c[2]) for c in http.calls] == [(a, "93.184.215.1"), (b, "93.184.215.2")]


@pytest.mark.parametrize(
    "url, ip, expected",
    [
        ("https://boards.example/jobs/1?x=2", "93.184.215.20", ("https://93.184.215.20/jobs/1?x=2", "boards.example")),
        ("https://boards.example:8443/a", "93.184.215.20", ("https://93.184.215.20:8443/a", "boards.example:8443")),
        ("https://boards.example/a", "2606:2800:220:1::1", ("https://[2606:2800:220:1::1]/a", "boards.example")),
    ],
)
def test_pinned_url(url, ip, expected):
    assert pinned_url(url, ip) == expected


def test_pinned_adapter_verifies_the_hostname_not_the_ip():
    adapter = PinnedAdapter("boards.example")
    kw = adapter.poolmanager.connection_pool_kw
    assert kw["server_hostname"] == "boards.example" and kw["assert_hostname"] == "boards.example"
    pool = adapter.poolmanager.connection_from_url("https://93.184.215.20/")
    assert pool.host == "93.184.215.20" and pool.assert_hostname == "boards.example"
    assert pool.conn_kw["server_hostname"] == "boards.example"


def test_body_is_capped(monkeypatch):
    monkeypatch.setattr("story_watch.jobs.MAX_BYTES", 10)
    http = FakeTransport({"https://boards.example/": Resp(body="x" * 100)})
    _, _, body = SafeFetcher(resolve=resolver({}), transport=http).get("https://boards.example/")
    assert body == "x" * 10


def test_no_cookies_or_instagram_headers_are_sent():
    http = FakeTransport({"https://boards.example/": Resp(body=OG)})
    f = SafeFetcher(resolve=resolver({}), transport=http)
    f.get("https://boards.example/")
    f.get("https://boards.example/")
    seen = []
    f.transport = lambda m, u, ip, headers, t, b=None: seen.append(headers) or Resp(body=OG)
    f.get("https://boards.example/")
    assert not any(k.lower() == "cookie" for k in seen[0]) and seen[0]["Sec-Fetch-Mode"] == "navigate"


# -- posting extraction -----------------------------------------------------------------------------

LONG = "<p>" + "You will build backend services. Qualifications: Python. Requirements: none. " * 10 + "</p>"


def test_greenhouse_api_posting():
    api = {"title": "Software Engineer Intern", "company_name": "Acme", "location": {"name": "Toronto, ON"},
           "content": html.escape(LONG + "<p>We are unable to sponsor visas.</p>")}
    http = FakeTransport({GH_API: Resp(body=api), GH: Resp(body=OG)})
    got = JobTitles(resolve=resolver({}), transport=http).lookup(GH)
    assert got.status == "ok" and got.source == "greenhouse" and got.read_ok
    assert got.title == "SWE Intern" and got.company == "Acme"  # page metadata first, API company
    assert got.locations == ("Toronto, ON",) and "unable to sponsor" in got.description


def test_greenhouse_api_404_is_closed():
    http = FakeTransport({GH: Resp(body=OG)})
    assert JobTitles(resolve=resolver({}), transport=http).lookup(GH).status == "closed"


def test_workday_cxs_posting():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Toronto/SWE-Intern_JR1"
    cxs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/Toronto/SWE-Intern_JR1"
    data = {"jobPostingInfo": {"title": "SWE Intern", "location": "Toronto", "timeType": "Full time",
                               "jobDescription": LONG, "country": {"descriptor": "Canada"}},
            "hiringOrganization": {"name": "2100 Acme Canada"}}
    http = FakeTransport({cxs: Resp(body=data)})
    got = JobTitles(resolve=resolver({}), transport=http).lookup(url)
    assert got.status == "ok" and got.source == "workday" and got.locations == ("Toronto", "Canada")
    assert got.company == "Acme Canada" and got.title == "SWE Intern"


def test_lever_and_ashby_postings():
    lever_url = "https://jobs.lever.co/acme/5c1d2e3f-0000-4000-8000-00000000abcd"
    lever_api = "https://api.lever.co/v0/postings/acme/5c1d2e3f-0000-4000-8000-00000000abcd"
    lever_data = {"text": "Backend Intern", "categories": {"location": "Remote, Canada", "commitment": "Intern"},
                  "descriptionPlain": html_text(LONG), "lists": []}
    ashby_url = "https://jobs.ashbyhq.com/acme/0b0e6a33-1111-4222-8333-944455556666"
    ashby_api = "https://api.ashbyhq.com/posting-api/job-board/acme"
    ashby_data = {"jobs": [{"id": "0b0e6a33-1111-4222-8333-944455556666", "title": "ML Intern", "location": "New York",
                            "descriptionPlain": html_text(LONG)}]}
    http = FakeTransport({lever_api: Resp(body=lever_data), ashby_api: Resp(body=ashby_data)})
    jt = JobTitles(resolve=resolver({}), transport=http)
    lv, ab = jt.lookup(lever_url), jt.lookup(ashby_url)
    assert (lv.source, lv.status, lv.employment_type, lv.locations) == ("lever", "ok", "Intern", ("Remote, Canada",))
    assert (ab.source, ab.status, ab.title, ab.locations) == ("ashby", "ok", "ML Intern", ("New York",))
    missing = "https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000000"
    assert jt.lookup(missing).status == "closed"


def test_jsonld_description_and_text_fallback():
    url = "https://careers.example.com/jobs/12345"
    page = ld({"@type": "JobPosting", "title": "Data Engineer, New Grad", "hiringOrganization": {"name": "Acme"},
               "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX"}}, "description": LONG})
    got = JobTitles(resolve=resolver({}), transport=FakeTransport({url: Resp(body=page)})).lookup(url)
    assert (got.source, got.status, got.locations, got.company) == ("jsonld", "ok", ("Austin, TX",), "Acme")
    text_page = "<html><body>" + LONG * 3 + "</body></html>"
    got = JobTitles(resolve=resolver({}), transport=FakeTransport({url: Resp(body=text_page)})).lookup(url)
    assert (got.source, got.status) == ("html", "ok")


def test_closed_and_javascript_pages():
    url = "https://careers.example.com/jobs/12345"
    moved = FakeTransport({url: Resp(status=302, location="https://careers.example.com/jobs"),
                           "https://careers.example.com/jobs": Resp(body=LONG * 3)})
    assert JobTitles(resolve=resolver({}), transport=moved).lookup(url).status == "closed"
    shell = FakeTransport({url: Resp(body="<html><script>app()</script><div id=root></div></html>")})
    assert JobTitles(resolve=resolver({}), transport=shell).lookup(url).status == "failed"
    gone = FakeTransport({url: Resp(body="<p>This job is no longer available.</p>" + LONG * 3)})
    assert JobTitles(resolve=resolver({}), transport=gone).lookup(url).status == "closed"


def test_event_page_is_read_and_flagged():
    url = "https://careers.example.com/jobs/events/777"
    page = "<p>" + "Join our virtual info session for students. Register below to save your seat. " * 8 + "</p>"
    got = JobTitles(resolve=resolver({}), transport=FakeTransport({url: Resp(body=page)})).lookup(url)
    assert got.status == "ok" and got.is_event_page


def test_company_page_with_an_embedded_greenhouse_board():
    url = "https://www.example.com/careers/?gh_jid=4444"
    page = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
    api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4444"
    http = FakeTransport({url: Resp(body=page), api: Resp(body={"title": "SWE Intern", "content": html.escape(LONG)})})
    got = JobTitles(resolve=resolver({}), transport=http).lookup(url)
    assert got.source == "greenhouse" and got.status == "ok" and http.urls == [url, api]


def test_lookup_never_raises_on_bad_json():
    http = FakeTransport({GH_API: Resp(body="{not json"), GH: Resp(body=OG)})
    assert JobTitles(resolve=resolver({}), transport=http).lookup(GH).title == "SWE Intern"


def html_text(markup):
    from story_watch.jobs import html_to_text

    return html_to_text(markup)


@pytest.mark.parametrize(
    "page, expected",
    [
        ("<title>Job Application for Software Engineering Intern (Summer 2027) at Sigma Computing</title>", "Sigma Computing"),
        ("<title>Software Engineering Intern (Winter/Spring) @ Constellation Space</title>", "Constellation Space"),
        (ld({"@type": "JobPosting", "hiringOrganization": {"name": "2100 NVIDIA USA"}}), "NVIDIA"),
        (ld({"@type": "JobPosting", "hiringOrganization": {"name": "Bose Corporation, U.S.A"}}), "Bose"),
        (ld({"@type": "JobPosting", "hiringOrganization": "HP"}), "HP"),
        # Citi's LD names a programme; fall through to the "| Citi Careers" title suffix
        (ld({"@type": "JobPosting", "hiringOrganization": {"name": "Early Career"}})
         + "<title>Junior Generative AI Application Developer | Citi Careers</title>", "Citi"),
        ('<meta property="og:site_name" content="Amazon.jobs"><title>SDE Intern - </title>', "Amazon"),
        ('<meta property="og:site_name" content="IBM">', "IBM"),
        ("<title>Otter.ai Careers - Shape the Future of Work</title>", None),
        ("", None),
    ],
)
def test_company_from_html(page, expected):
    assert company_from_html(page) == expected


def test_add_job_info_reads_a_non_job_link_for_the_tagger_only():
    from dataclasses import replace as dc_replace

    from conftest import make_item

    from story_watch.jobs import add_job_info

    url = "https://events.example.com/e/777"
    page = "<title>Info session</title><p>" + "Join our virtual info session. Register below. " * 12 + "</p>"
    jt = JobTitles(resolve=resolver({}), transport=FakeTransport({url: Resp(body=page)}))
    item = add_job_info(dc_replace(make_item("1"), links=(url, "http://plain.example/")), jt)
    assert item.job_title is None and item.company is None  # the embed stays "New story"
    assert item.posting.status == "ok" and item.posting.is_event_page
    assert add_job_info(dc_replace(make_item("2"), links=("http://plain.example/",)), jt).posting is None


def test_lookup_never_raises_on_deeply_nested_json_ld():
    page = '<script type="application/ld+json">' + "[" * 100_000 + "</script>" + OG
    http = FakeTransport({GH: Resp(body=page)})
    assert JobTitles(resolve=resolver({}), transport=http).lookup(GH).title == "SWE Intern"


def test_api_json_of_an_unexpected_shape_does_not_raise():
    lever_url = "https://jobs.lever.co/acme/5c1d2e3f-0000-4000-8000-00000000abcd"
    lever_api = "https://api.lever.co/v0/postings/acme/5c1d2e3f-0000-4000-8000-00000000abcd"
    http = FakeTransport({lever_api: Resp(body={"text": "x", "lists": 5}), lever_url: Resp(body=OG)})
    got = JobTitles(resolve=resolver({}), transport=http).lookup(lever_url)
    assert (got.title, got.status) == ("SWE Intern", "failed")


@pytest.mark.parametrize("unit", ["<meta", "<script", '<script type="application/ld+json">',
                                  '<script type="application/json">', "<title>"])
def test_extractors_stay_linear_on_unclosed_tags(unit):
    import time

    from story_watch.jobs import posting_from_app_json, posting_from_html, posting_from_jsonld

    page = unit * (2_000_000 // len(unit))  # a full-size page of unclosed tags
    start = time.monotonic()
    title_from_html(page), company_from_html(page), posting_from_jsonld(page), posting_from_app_json(page)
    posting_from_html(page[:200_000])
    assert time.monotonic() - start < 10  # quadratic scans took hours


def test_meta_and_title_parsing_keeps_attribute_order_and_case():
    assert title_from_html('<META content="A Role" name="og:title"><title>B</title>') == "A Role"
    assert title_from_html('<meta property="og:title"><TITLE>Plain Title</TITLE >') == "Plain Title"
    assert company_from_html('<meta content="Acme" property="og:site_name">') == "Acme"


def test_a_slow_lookup_is_given_up_and_not_restarted(monkeypatch):
    import threading

    release = threading.Event()
    calls = []

    def stalled(*a, **kw):
        calls.append(1)
        release.wait(5)
        raise OSError("gave up")

    jt = JobTitles(resolve=resolver({}), transport=stalled, budget=0.05)
    url = "https://careers.example.com/jobs/12345"
    got = jt.lookup(url)
    assert got.status == "failed" and got.description == ""
    jt.start_cycle()
    assert jt.lookup(url).status == "failed" and len(calls) == 1  # still stuck: not started again
    release.set()


def test_add_job_info_never_raises():
    from dataclasses import replace as dc_replace

    from conftest import make_item

    from story_watch.jobs import add_job_info

    class Broken:
        def lookup(self, url):
            raise RuntimeError("boom")

    item = dc_replace(make_item("1"), links=("https://careers.example.com/jobs/1",))
    assert add_job_info(item, Broken()) is item
