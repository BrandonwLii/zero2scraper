import json

import pytest

from story_watch.jobs import JobTitles, is_job_link, title_from_html, title_from_slug


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
    assert title_from_slug("https://jobs.lever.co/shieldai/71a4617f-c917-4536-bbf1-8d11d04d8cba") is None
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
        self._body = body.encode()

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.routes[url]


def resolver(ips):
    def resolve(host, port, proto=0):
        return [(2, 1, 6, "", (ips.get(host, "93.184.215.14"), port))]

    return resolve


GH = "https://job-boards.greenhouse.io/acme/jobs/1"
OG = '<meta property="og:title" content="SWE Intern">'


def test_lookup_fetches_and_caches():
    http = FakeHTTP({GH: Resp(body=OG)})
    jt = JobTitles(session=http, resolve=resolver({}))
    assert jt.lookup(GH) == "SWE Intern"
    assert jt.lookup(GH) == "SWE Intern"
    assert http.calls == [GH]


def test_lookup_refuses_private_addresses_and_http():
    http = FakeHTTP({})
    jt = JobTitles(session=http, resolve=resolver({"job-boards.greenhouse.io": "10.0.0.78"}))
    assert jt.lookup(GH) is None
    assert jt.lookup("http://example.com/jobs/1") is None
    assert http.calls == []


def test_redirect_to_private_address_is_not_followed():
    http = FakeHTTP({GH: Resp(status=302, location="https://router.lan/jobs/1")})
    jt = JobTitles(session=http, resolve=resolver({"router.lan": "192.168.1.1"}))
    assert jt.lookup(GH) is None
    assert http.calls == [GH]


def test_redirect_is_followed():
    final = "https://boards.example/jobs/1"
    http = FakeHTTP({GH: Resp(status=301, location=final), final: Resp(body=OG)})
    assert JobTitles(session=http, resolve=resolver({})).lookup(GH) == "SWE Intern"


def test_error_page_falls_back_to_slug():
    url = "https://x.icims.com/jobs/1/intern,-applied-ai-development/job"
    http = FakeHTTP({url: Resp(status=403)})
    assert JobTitles(session=http, resolve=resolver({})).lookup(url) == "Intern, applied ai development"
