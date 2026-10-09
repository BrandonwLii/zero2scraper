"""Turn a job link into a Posting: title, company, locations and the description as plain text.

One extractor per source. `extract(url, fetcher)` picks the ATS API by host when it
knows one, and falls back to the HTML page (JSON-LD JobPosting, then visible text).
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fetch import Fetcher

MIN_DESCRIPTION = 400  # chars; shorter "descriptions" are teasers or empty shells


@dataclass
class Posting:
    source: str  # greenhouse | lever | ashby | workday | smartrecruiters | jsonld | html | none
    status: str = "ok"  # ok | dead | blocked | error | no_description
    title: str | None = None
    company: str | None = None
    locations: list[str] = field(default_factory=list)
    remote: bool | None = None
    employment_type: str | None = None
    text: str = ""  # description, plain text
    notes: list[str] = field(default_factory=list)

    @property
    def has_description(self) -> bool:
        return self.status == "ok" and len(self.text) >= MIN_DESCRIPTION


# -- HTML to text ---------------------------------------------------------------------------

_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
          "header", "footer", "table", "dd", "dt"}
_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(markup: str | None, _again: bool = True) -> str:
    if not markup:
        return ""
    parser = _Text()
    parser.feed(markup)
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    text = "\n".join(line for line in lines if line)
    if _again and re.search(r"</?(?:p|li|ul|br|div|h\d)\b", text):  # double-escaped HTML (seen in JSON-LD)
        return html_to_text(text, _again=False)
    return text


# -- JSON-LD --------------------------------------------------------------------------------

_LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _ld_job_postings(page: str):
    for block in _LD_JSON.findall(page):
        try:
            stack = [json.loads(block.strip())]
        except ValueError:
            continue
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                kind = obj.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    yield obj
                else:
                    stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))


def _ld_place(place: Any) -> str | None:
    if isinstance(place, str):
        return place
    if not isinstance(place, dict):
        return None
    addr = place.get("address", place)
    if isinstance(addr, str):
        return addr
    if not isinstance(addr, dict):
        return place.get("name") if isinstance(place.get("name"), str) else None
    country = addr.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
    text = ", ".join(p for p in parts if isinstance(p, str) and p.strip())
    return text or (place.get("name") if isinstance(place.get("name"), str) else None)


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def posting_from_jsonld(page: str) -> Posting | None:
    for ld in _ld_job_postings(page):
        org = ld.get("hiringOrganization")
        locs = [_ld_place(p) for p in _as_list(ld.get("jobLocation"))]
        locs += ["applicant location: " + s for s in map(_ld_place, _as_list(ld.get("applicantLocationRequirements"))) if s]
        etype = ld.get("employmentType")
        return Posting(
            source="jsonld",
            title=ld.get("title") if isinstance(ld.get("title"), str) else None,
            company=(org.get("name") if isinstance(org, dict) else org) if org else None,
            locations=[l for l in locs if l],
            remote=True if ld.get("jobLocationType") == "TELECOMMUTE" else None,
            employment_type=", ".join(etype) if isinstance(etype, list) else etype,
            text=html_to_text(html.unescape(ld.get("description") or "")),
        )
    return None


# -- JSON embedded in the page (server-rendered app state) --------------------------------------

_SCRIPT_JSON = re.compile(r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_POSTING_KEYS = ("responsibilities", "minimum_qualifications", "preferred_qualifications", "description")


def _walk_posting(obj: Any, depth: int = 0) -> dict | None:
    if depth > 60:
        return None
    if isinstance(obj, dict):
        if "title" in obj and sum(k in obj for k in _POSTING_KEYS) >= 2:
            return obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                hit = _walk_posting(v, depth + 1)
                if hit:
                    return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _walk_posting(v, depth + 1)
            if hit:
                return hit
    return None


def _flatten(v: Any) -> str:
    if isinstance(v, str):
        return html_to_text(v) if "<" in v else v
    if isinstance(v, list):
        return "\n".join(_flatten(x) for x in v)
    if isinstance(v, dict):
        return "\n".join(_flatten(x) for x in v.values())
    return ""


def posting_from_app_json(page: str) -> Posting | None:
    """A posting object in <script type="application/json"> state (seen on one big-tech careers site)."""
    for block in _SCRIPT_JSON.findall(page):
        try:
            hit = _walk_posting(json.loads(block))
        except ValueError:
            continue
        if hit:
            locs = hit.get("locations") or []
            text = "\n".join(f"{k}:\n{_flatten(hit.get(k))}" for k in _POSTING_KEYS if hit.get(k))
            return Posting(source="app_json", title=hit.get("title") if isinstance(hit.get("title"), str) else None,
                           locations=[_flatten(l) for l in locs] if isinstance(locs, list) else [], text=text)
    return None


# -- visible text fallback --------------------------------------------------------------------

_POSTING_WORDS = re.compile(r"qualifications|responsibilit|requirements|what you.ll|you will|about the role|"
                            r"who you are|minimum|preferred", re.I)
_DEAD = re.compile(r"no longer (available|accepting|open)|position has been filled|job (is )?(closed|expired)|"
                   r"page (you|was) .{0,30}not (be )?found|couldn.t find (that|this) (job|page)", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def posting_from_html(page: str) -> Posting:
    text = html_to_text(page)
    title = _TITLE.search(page)
    p = Posting(source="html", title=" ".join(html.unescape(title.group(1)).split()) if title else None, text=text)
    if _DEAD.search(text[:5000]):
        p.status = "dead"
    elif len(text) < 1500 or len(_POSTING_WORDS.findall(text)) < 2:
        p.status = "no_description"
        p.notes.append(f"visible text {len(text)} chars")
    return p


# -- ATS APIs -----------------------------------------------------------------------------------

def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def greenhouse(url: str, f: Fetcher) -> Posting | None:
    host, seg = _host(url), _segments(url)
    if not host.endswith("greenhouse.io") or "jobs" not in seg:
        return None
    i = seg.index("jobs")
    if i == 0 or i + 1 >= len(seg):
        return None
    board, job = seg[i - 1], seg[i + 1]
    api = "boards-api.eu.greenhouse.io" if ".eu." in host else "boards-api.greenhouse.io"
    status, d = f.get_json(f"https://{api}/v1/boards/{quote(board)}/jobs/{quote(job)}")
    if status == 404:
        return Posting(source="greenhouse", status="dead")
    if status != 200 or not isinstance(d, dict):
        return Posting(source="greenhouse", status="error", notes=[f"HTTP {status}"])
    return Posting(
        source="greenhouse",
        title=d.get("title"),
        company=d.get("company_name"),
        locations=[(d.get("location") or {}).get("name")] if (d.get("location") or {}).get("name") else [],
        text=html_to_text(html.unescape(d.get("content") or "")),
    )


def lever(url: str, f: Fetcher) -> Posting | None:
    host, seg = _host(url), _segments(url)
    if not host.endswith("lever.co") or len(seg) < 2:
        return None
    api = "api.eu.lever.co" if ".eu." in host else "api.lever.co"
    status, d = f.get_json(f"https://{api}/v0/postings/{quote(seg[0])}/{quote(seg[1])}")
    if status == 404:
        return Posting(source="lever", status="dead")
    if status != 200 or not isinstance(d, dict):
        return Posting(source="lever", status="error", notes=[f"HTTP {status}"])
    cats = d.get("categories") or {}
    lists = "\n".join(f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in d.get("lists") or [])
    return Posting(
        source="lever",
        title=d.get("text"),
        locations=list(dict.fromkeys([cats.get("location")] + list(cats.get("allLocations") or []))) if cats.get("location") else [],
        remote=d.get("workplaceType") == "remote" if d.get("workplaceType") else None,
        employment_type=cats.get("commitment"),
        text="\n".join(x for x in (d.get("descriptionPlain"), lists, d.get("additionalPlain")) if x),
    )


def ashby(url: str, f: Fetcher) -> Posting | None:
    host, seg = _host(url), _segments(url)
    if host != "jobs.ashbyhq.com" or len(seg) < 2:
        return None
    board, job = seg[0], seg[1]
    status, d = f.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(board)}?includeCompensation=true")
    if status != 200 or not isinstance(d, dict):
        return Posting(source="ashby", status="error", notes=[f"HTTP {status}"])
    for j in d.get("jobs") or []:
        if j.get("id") == job:
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
            country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry"))
            if country:
                locs.append(country)
            return Posting(
                source="ashby",
                title=j.get("title"),
                locations=[l for l in locs if l],
                remote=j.get("isRemote"),
                employment_type=j.get("employmentType"),
                text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            )
    return Posting(source="ashby", status="dead", notes=["id not on the public board (closed or unlisted)"])


_WD_HOST = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
_LANG = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def workday(url: str, f: Fetcher) -> Posting | None:
    host, seg = _host(url), _segments(url)
    m = _WD_HOST.match(host)
    if not m:
        return None
    if seg and _LANG.match(seg[0]):
        seg = seg[1:]
    if len(seg) < 3 or "job" not in seg:
        return None
    site, rest = seg[0], seg[seg.index("job"):]
    status, d = f.get_json(f"https://{host}/wday/cxs/{m.group(1)}/{site}/" + "/".join(quote(s) for s in rest))
    if status == 404:
        return Posting(source="workday", status="dead")
    if status != 200 or not isinstance(d, dict) or "jobPostingInfo" not in d:
        return Posting(source="workday", status="error", notes=[f"HTTP {status}"])
    info = d["jobPostingInfo"]
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    if (info.get("country") or {}).get("descriptor"):
        locs.append(info["country"]["descriptor"])
    return Posting(
        source="workday",
        title=info.get("title"),
        company=(d.get("hiringOrganization") or {}).get("name"),
        locations=[l for l in locs if l],
        employment_type=info.get("timeType"),
        text=html_to_text(info.get("jobDescription")),
    )


def smartrecruiters(url: str, f: Fetcher) -> Posting | None:
    host, seg = _host(url), _segments(url)
    if not host.endswith("smartrecruiters.com") or len(seg) < 2:
        return None
    job = seg[1].split("-", 1)[0]
    status, d = f.get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(seg[0])}/postings/{quote(job)}")
    if status == 404:
        return Posting(source="smartrecruiters", status="dead")
    if status != 200 or not isinstance(d, dict):
        return Posting(source="smartrecruiters", status="error", notes=[f"HTTP {status}"])
    loc = d.get("location") or {}
    sections = ((d.get("jobAd") or {}).get("sections") or {})
    return Posting(
        source="smartrecruiters",
        title=d.get("name"),
        company=(d.get("company") or {}).get("name"),
        locations=[", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)],
        remote=loc.get("remote"),
        employment_type=(d.get("typeOfEmployment") or {}).get("label"),
        text="\n".join(html_to_text(s.get("text")) for s in sections.values() if isinstance(s, dict)),
    )


# Company career sites that embed an ATS: the page links or iframes the board.
_EMBED = [
    (re.compile(r"boards(?:-api)?(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=|v1/boards/)([\w-]+)[^\"']*?(?:token=|/jobs/)(\d+)"),
     lambda m: f"https://job-boards.greenhouse.io/{m.group(1)}/jobs/{m.group(2)}"),
    (re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)/([0-9a-f-]{36})"), lambda m: f"https://jobs.lever.co/{m.group(1)}/{m.group(2)}"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})"), lambda m: f"https://jobs.ashbyhq.com/{m.group(1)}/{m.group(2)}"),
]

API_EXTRACTORS = (greenhouse, lever, ashby, workday, smartrecruiters)


_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f-]{27}", re.I)
_GH_BOARD = re.compile(r"greenhouse\.io/(?:v1/boards|embed/job_board/js\?for=|embed/job_app\?for=)/?([\w-]+)")


def _redirected_away(url: str, final: str) -> bool:
    """A posting URL that redirects to a page without the job's id: closed, sent to the listing."""
    ids = _ID.findall(urlsplit(url).path + "?" + urlsplit(url).query)
    return bool(ids) and final != url and not any(i in final for i in ids)


def from_page(url: str, f: Fetcher, embedded: bool = False) -> Posting:
    """JSON-LD, an embedded ATS board, or the visible text of the page itself."""
    if (urlsplit(url).hostname or "").endswith(".icims.com"):
        # iCIMS renders the posting in an iframe; ?in_iframe=1 is that iframe's document.
        url = url.split("?", 1)[0] + "?in_iframe=1"
    status, final, page = f.get(url)
    if status in (404, 410):
        return Posting(source="html", status="dead", notes=[f"HTTP {status}"])
    if status != 200:
        return Posting(source="html", status="error", notes=[f"HTTP {status}"])
    if _redirected_away(url, final):
        return Posting(source="html", status="dead", notes=["redirected to a page without the job id"])
    gh_jid = parse_qs(urlsplit(final).query).get("gh_jid")
    board = _GH_BOARD.search(page)
    if gh_jid and board:
        p = greenhouse(f"https://job-boards.greenhouse.io/{board.group(1)}/jobs/{gh_jid[0]}", f)
        if p is not None:
            p.notes.append("gh_jid on a company page; board name found in the page")
            return p
    for rx, to_url in _EMBED:
        m = None if embedded else rx.search(page)
        if m and _host(to_url(m)) != _host(final):
            p = extract(to_url(m), f, embedded=True)
            p.notes.append(f"embedded {p.source} board found in the company page")
            return p
    ld = posting_from_jsonld(page)
    if ld is not None and len(ld.text) >= MIN_DESCRIPTION:
        return ld
    app = posting_from_app_json(page)
    if app is not None and len(app.text) >= MIN_DESCRIPTION:
        return app
    p = posting_from_html(page)
    if ld is not None:  # JSON-LD without a usable description: keep its fields, use page text
        ld.source, ld.status, ld.text = "jsonld+html", p.status, p.text
        ld.notes.append("JSON-LD description missing or short")
        return ld
    if p.status == "no_description":
        p.notes.append("likely needs JavaScript or a login")
    return p


def extract(url: str, f: Fetcher, embedded: bool = False) -> Posting:
    """ATS API when the host has one; if it fails or has no text, the HTML page."""
    api = None
    for ex in API_EXTRACTORS:
        try:
            api = ex(url, f)
        except OSError as e:  # e.g. an API host that doesn't resolve
            api = Posting(source=ex.__name__, status="error", notes=[type(e).__name__])
        if api is not None:
            break
    if api is not None and api.status == "ok" and api.has_description:
        return api
    page = from_page(url, f, embedded)
    if api is None or page.has_description or page.status == "ok":
        if api is not None:
            page.notes.append(f"{api.source} API: {api.status} {' '.join(api.notes)}".strip())
        return page
    if api.status == "ok":
        api.status = "no_description"
    api.notes.append(f"page: {page.status} {' '.join(page.notes)}".strip())
    return api
