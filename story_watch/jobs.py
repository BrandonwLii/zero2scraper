"""Read the job posting behind a story's link sticker: title, company, locations and text (#9).

Title and company come from the page's metadata, as before: JSON-LD JobPosting.title, then
og:title, then <title>, then a title-ish URL slug. The posting text comes from the ATS's public
API when the host has one (Workday cxs, Greenhouse, Lever, Ashby), else from the page itself
(JSON-LD description, server-rendered app state, the iCIMS iframe, then the visible text). The
tagger (story_watch/hybrid.py) reads sponsorship and level from that text.

Pages come from arbitrary third-party sites and the service runs inside a home network, so every
request goes through ``SafeFetcher``: a fresh session that never carries Instagram cookies (or any
cookies), https only, redirects followed by hand with a public-address check on every hop, and a
size cap. The connection goes to the very address that was checked (the DNS answer is pinned), with
SNI and certificate verification still done for the hostname, so a second, different DNS answer
(DNS rebinding) can't point a request at the local network.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
import threading
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from .instagram import StoryItem

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# Sec-Fetch-* are what any browser sends on navigation; one big-tech careers site answers 400 without them (#9).
NAV_HEADERS = {"Accept-Language": "en-US,en;q=0.5", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
               "Sec-Fetch-Site": "none"}
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
MAX_TITLE = 200
MAX_DESCRIPTION = 50_000  # chars kept per posting; the rules read the whole text, the prompt only its start
MIN_DESCRIPTION = 400  # shorter "descriptions" are teasers or empty shells

_JOB_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "icims.com", "smartrecruiters.com",
              "workable.com", "oraclecloud.com", "jobvite.com", "bamboohr.com", "recruitee.com")
_JOB_WORDS = re.compile(r"(^|[/._?&=-])(jobs?|careers?|gh_jid|jobid|job-detail|positions?|openings?)([/._?&=-]|$)", re.I)

# Pages are attacker-controlled and up to MAX_BYTES, so tags are found in linear time: an opening tag
# can't contain "<" (a "[^>]*" pattern, or "(.*?)</script>" without a closing tag, is quadratic on
# a page of thousands of unclosed tags), and each body ends at the first closing tag after it.
_LD_TYPE = re.compile(r"application/ld\+json", re.I)
_JSON_TYPE = re.compile(r"""type=["']application/json["']""", re.I)
_OG_TITLE = re.compile(r"""(?:property|name)=["']og:title["']""", re.I)
_SITE_NAME = re.compile(r"""property=["']og:site_name["']""", re.I)
_CONTENT = re.compile(r"""content=["']([^"']*)["']""", re.I)
_META = re.compile(r"<meta\b([^<>]*)>", re.I)
_GREENHOUSE_TITLE = re.compile(r"^Job Application for (.+) at .+$")
# og:title/<title> of a careers landing page rather than a posting (JSON-LD is trusted).
_GENERIC_PAGE = re.compile(r"\bcareers?\b|\bcareer site\b|\bjob search\b|^jobs?$", re.I)
_HOSTED_AT = re.compile(r"^(?:Job Application for )?.+ (?:at|@) (.+)$")  # Greenhouse "at X", Ashby "@ X"
_ORG_CODE = re.compile(r"^(?:[A-Z]*\d[A-Z\d]*\s+)+")  # Workday entity codes: "2100 NVIDIA USA", "IF1690 ..."
_ORG_SUFFIX = re.compile(
    r"(?:,?\s+(?:U\.?S\.?A?\.?|LLC|L\.?P\.?|Inc\.?|Corp\.?|Corporation|Ltd\.?|Careers?|Jobs))+$|\.jobs$", re.I
)
# hiringOrganization values that name a hiring programme, not the company (Citi: "Early Career").
_NOT_A_COMPANY = re.compile(r"career|universit|campus|student|graduate|talent|recruit|intern", re.I)
_MAX_COMPANY = 60


_ID_LIKE = re.compile(r"^(?:[0-9a-f-]{16,}|[A-Z]{0,3}\d[\w-]*)$", re.I)


def is_job_link(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _JOB_HOSTS):
        return True
    return bool(_JOB_WORDS.search(f"{host}{parts.path}?{parts.query}"))


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(html.unescape(text).split())
    return text[:MAX_TITLE] or None


def _ld_job_posting(objs: Any) -> dict[str, Any] | None:
    stack = [objs]
    while stack:
        obj = stack.pop()
        if isinstance(obj, list):
            stack.extend(obj)
        elif isinstance(obj, dict):
            kind = obj.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return obj
            stack.extend(v for k, v in obj.items() if k == "@graph" or isinstance(v, (dict, list)))
    return None


def _elements(page: str, tag: str, attrs: Optional[re.Pattern] = None) -> Iterator[str]:
    """The bodies of ``<tag ...>...</tag>`` elements whose attributes match ``attrs``, in order."""
    opening = re.compile(rf"<{tag}\b([^<>]*)>", re.I)
    closing = re.compile(rf"</{tag}\s*>", re.I)
    pos = 0
    while True:
        start = opening.search(page, pos)
        if start is None:
            return
        end = closing.search(page, start.end())
        if end is None:
            return
        if attrs is None or attrs.search(start.group(1)):
            yield page[start.end():end.start()]
        pos = end.end()


def _meta_content(page: str, name: re.Pattern) -> Optional[str]:
    for tag in _META.finditer(page):
        if name.search(tag.group(1)):
            content = _CONTENT.search(tag.group(1))
            if content:
                return content.group(1)
    return None


def _ld_postings(page: str):
    for block in _elements(page, "script", _LD_TYPE):
        try:
            posting = _ld_job_posting(json.loads(block.strip()))
        except (ValueError, RecursionError):
            continue
        if posting:
            yield posting


def _page_title(page: str) -> str | None:
    return _clean(next(_elements(page, "title"), None))


def title_from_html(page: str) -> str | None:
    for posting in _ld_postings(page):
        title = _clean(posting.get("title") if isinstance(posting.get("title"), str) else None)
        if title:
            return title
    candidates = []
    og = _meta_content(page, _OG_TITLE)
    if og is not None:
        candidates.append(_clean(og))
    title = _page_title(page)
    if title:
        gh = _GREENHOUSE_TITLE.match(title)
        candidates.append(gh.group(1) if gh else title)
    for title in candidates:
        if title and not _GENERIC_PAGE.search(title):
            return title
    return None


def clean_company(name: str | None) -> str | None:
    name = _clean(name)
    if not name:
        return None
    name = _ORG_SUFFIX.sub("", _ORG_CODE.sub("", name)).strip(" ,-|")
    return name if name and len(name) <= _MAX_COMPANY else None


def company_from_html(page: str) -> str | None:
    """Hiring company: "at X" titles, JSON-LD hiringOrganization, og:site_name, then a "| X" title suffix."""
    title = _page_title(page)
    hosted = _HOSTED_AT.match(title or "")
    candidates = [hosted.group(1) if hosted else None]
    for posting in _ld_postings(page):
        org = posting.get("hiringOrganization")
        name = org.get("name") if isinstance(org, dict) else org
        if isinstance(name, str) and not _NOT_A_COMPANY.search(name):
            candidates.append(name)
    site = _meta_content(page, _SITE_NAME)
    if site is not None:
        candidates.append(site)
    if title and " | " in title:
        candidates.append(title.rsplit(" | ", 1)[1])
    for candidate in candidates:
        company = clean_company(candidate)
        if company:
            return company
    return None


def title_from_slug(url: str) -> str | None:
    """Best path segment that reads like words, e.g. iCIMS '/jobs/19550/intern,-applied-ai/job'."""
    best = None
    for seg in urlsplit(url).path.split("/"):
        words = [w for w in re.split(r"[-_+]+", unquote(seg)) if w]
        if len(words) < 3 or _ID_LIKE.match(seg) or sum(w.isalpha() for w in words) < 2:
            continue
        if best is None or len(words) > len(best):
            best = words
    if best is None:
        return None
    text = " ".join(best)
    return _clean(text[:1].upper() + text[1:])


# -- guarded fetch ------------------------------------------------------------------------------


class Blocked(ValueError):
    """A URL the guards refuse: not https, a non-public address, or too many redirects."""


def check_public(url: str, resolve: Callable[..., list] = socket.getaddrinfo) -> str:
    """The address to connect to for ``url``. Raises Blocked unless it is https and every
    address the name resolves to is public."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise Blocked("not an https URL")
    infos = resolve(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    if not infos:
        raise Blocked("does not resolve")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise Blocked("resolves to a non-public address")
    return infos[0][4][0]


_check_public = check_public  # archive.py uses the check alone


class PinnedAdapter(HTTPAdapter):
    """Connects to the URL's host (an IP literal), while TLS sends SNI for, and verifies the
    certificate against, ``hostname``."""

    def __init__(self, hostname: str):
        self._hostname = hostname
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self._hostname
        pool_kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def pinned_url(url: str, ip: str) -> tuple[str, str]:
    """(``url`` with its host replaced by ``ip``, the Host header for the original host)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parts.port and parts.port != 443:
        netloc_ip += f":{parts.port}"
        host += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc_ip)), host


def _pinned_send(method: str, url: str, ip: str, headers: dict, timeout: float, body: Any = None) -> requests.Response:
    hostname = urlsplit(url).hostname or ""
    target, host_header = pinned_url(url, ip)
    req = requests.Request(method, target, headers={**headers, "Host": host_header}, json=body).prepare()
    adapter = PinnedAdapter(hostname)
    try:
        resp = adapter.send(req, stream=True, timeout=timeout, verify=True)  # never follows redirects
    except BaseException:
        adapter.close()
        raise
    resp._pinned_adapter = adapter  # closed with the response
    return resp


Transport = Callable[..., Any]  # (method, url, ip, headers, timeout, body) -> response


class SafeFetcher:
    """GET/POST with the guards in the module docstring. ``transport`` is for tests."""

    def __init__(self, timeout: float = 10, resolve: Callable[..., list] = socket.getaddrinfo,
                 transport: Transport = _pinned_send):
        self.timeout = timeout
        self.resolve = resolve
        self.transport = transport
        self.headers = {"User-Agent": USER_AGENT, **NAV_HEADERS}

    def get(self, url: str, accept: str = "text/html,*/*;q=0.8", data: Any = None) -> tuple[int, str, str]:
        """(status, final url, body text, capped at MAX_BYTES). Raises Blocked, requests or OS errors."""
        for _ in range(MAX_REDIRECTS + 1):
            ip = check_public(url, self.resolve)
            resp = self.transport("GET" if data is None else "POST", url, ip, {**self.headers, "Accept": accept},
                                  self.timeout, data)
            try:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("Location", ""))
                    continue
                body = b""
                for chunk in resp.iter_content(65536):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        body = body[:MAX_BYTES]
                        break
                return resp.status_code, url, body.decode(resp.encoding or "utf-8", "replace")
            finally:
                resp.close()
                adapter = getattr(resp, "_pinned_adapter", None)
                if adapter is not None:
                    adapter.close()
        raise Blocked("too many redirects")

    def get_json(self, url: str, data: Any = None) -> tuple[int, Any]:
        status, _, body = self.get(url, accept="application/json", data=data)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None


# -- the posting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobPosting:
    """What the link's page says. ``status``: ``ok`` (the text was read), ``closed`` (404, an ATS
    404, or a redirect away from the job), or ``failed`` (anything else: blocked, JavaScript-only,
    an HTTP error). ``title`` and ``company`` are filled even when the text couldn't be read."""

    title: Optional[str] = None
    company: Optional[str] = None
    status: str = "failed"
    source: str = "none"
    locations: tuple[str, ...] = ()
    employment_type: Optional[str] = None
    description: str = ""
    is_event_page: bool = False

    @property
    def read_ok(self) -> bool:
        return self.status == "ok" and len(self.description) >= 200


JobInfo = JobPosting  # the old name


@dataclass
class _Extract:
    source: str
    status: str = "ok"  # ok | dead | error | no_description
    title: Optional[str] = None
    company: Optional[str] = None
    locations: list[str] = field(default_factory=list)
    employment_type: Optional[str] = None
    text: str = ""
    is_event_page: bool = False

    @property
    def has_description(self) -> bool:
        return self.status == "ok" and len(self.text) >= MIN_DESCRIPTION


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


def html_to_text(markup: Optional[str], _again: bool = True) -> str:
    if not markup or not isinstance(markup, str):
        return ""
    parser = _Text()
    parser.feed(markup)
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    text = "\n".join(line for line in lines if line)
    if _again and re.search(r"</?(?:p|li|ul|br|div|h\d)\b", text):  # double-escaped HTML (seen in JSON-LD)
        return html_to_text(text, _again=False)
    return text


def _str(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v.strip() else None


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _ld_place(place: Any) -> Optional[str]:
    if isinstance(place, str):
        return place
    if not isinstance(place, dict):
        return None
    addr = place.get("address", place)
    if isinstance(addr, str):
        return addr
    if not isinstance(addr, dict):
        return _str(place.get("name"))
    country = addr.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
    text = ", ".join(p for p in parts if isinstance(p, str) and p.strip())
    return text or _str(place.get("name"))


def posting_from_jsonld(page: str) -> Optional[_Extract]:
    for ld in _ld_postings(page):
        org = ld.get("hiringOrganization")
        locs = [_ld_place(p) for p in _as_list(ld.get("jobLocation"))]
        locs += ["applicant location: " + s for s in map(_ld_place, _as_list(ld.get("applicantLocationRequirements"))) if s]
        etype = ld.get("employmentType")
        return _Extract(
            source="jsonld",
            title=_str(ld.get("title")),
            company=_str(org.get("name") if isinstance(org, dict) else org),
            locations=[loc for loc in locs if loc],
            employment_type=", ".join(e for e in etype if isinstance(e, str)) if isinstance(etype, list) else _str(etype),
            text=html_to_text(html.unescape(ld.get("description") or "") if isinstance(ld.get("description"), str) else ""),
        )
    return None


_POSTING_KEYS = ("responsibilities", "minimum_qualifications", "preferred_qualifications", "description")


def _walk_posting(obj: Any, depth: int = 0) -> Optional[dict]:
    if depth > 60:
        return None
    if isinstance(obj, dict):
        if "title" in obj and sum(k in obj for k in _POSTING_KEYS) >= 2:
            return obj
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        if isinstance(v, (dict, list)):
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


def posting_from_app_json(page: str) -> Optional[_Extract]:
    """A posting object in <script type="application/json"> state (one big-tech careers site, #9)."""
    for block in _elements(page, "script", _JSON_TYPE):
        try:
            hit = _walk_posting(json.loads(block))
        except (ValueError, RecursionError):
            continue
        if hit:
            locs = hit.get("locations") or []
            text = "\n".join(f"{k}:\n{_flatten(hit.get(k))}" for k in _POSTING_KEYS if hit.get(k))
            return _Extract(source="app_json", title=_str(hit.get("title")),
                            locations=[_flatten(loc) for loc in locs] if isinstance(locs, list) else [], text=text)
    return None


_POSTING_WORDS = re.compile(r"qualifications|responsibilit|requirements|what you.ll|you will|about the role|"
                            r"who you are|minimum|preferred", re.I)
_DEAD = re.compile(r"no longer (available|accepting|open)|position has been filled|job (is )?(closed|expired)|"
                   r"page (you|was) .{0,30}not (be )?found|couldn.t find (that|this) (job|page)", re.I)
_EVENT_PAGE = re.compile(r"\binfo(?:rmation)? session|\bwebinar|\bhackathon|\bcareer fair|\bworkshop|\bconference|"
                         r"\bregist(?:er|ration)\b|\bRSVP\b|\bopen house|\bcoffee chat|\bnetworking event", re.I)


def posting_from_html(page: str) -> _Extract:
    text = html_to_text(page)
    p = _Extract(source="html", title=_page_title(page), text=text)
    if _DEAD.search(text[:5000]):
        p.status = "dead"
    elif len(text) < 1500 or len(_POSTING_WORDS.findall(text)) < 2:
        if len(text) >= MIN_DESCRIPTION and _EVENT_PAGE.search(text[:3000]):
            p.is_event_page = True  # readable, but an event registration page rather than a job
        else:
            p.status = "no_description"
    return p


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def greenhouse(url: str, f: SafeFetcher) -> Optional[_Extract]:
    host, seg = _host(url), _segments(url)
    if not host.endswith("greenhouse.io") or "jobs" not in seg:
        return None
    i = seg.index("jobs")
    if i == 0 or i + 1 >= len(seg):
        return None
    api = "boards-api.eu.greenhouse.io" if ".eu." in host else "boards-api.greenhouse.io"
    status, d = f.get_json(f"https://{api}/v1/boards/{quote(seg[i - 1])}/jobs/{quote(seg[i + 1])}")
    if status == 404:
        return _Extract(source="greenhouse", status="dead")
    if status != 200 or not isinstance(d, dict):
        return _Extract(source="greenhouse", status="error")
    loc = d.get("location") if isinstance(d.get("location"), dict) else {}
    return _Extract(
        source="greenhouse",
        title=_str(d.get("title")),
        company=_str(d.get("company_name")),
        locations=[loc["name"]] if _str(loc.get("name")) else [],
        text=html_to_text(html.unescape(d.get("content") or "") if isinstance(d.get("content"), str) else ""),
    )


def lever(url: str, f: SafeFetcher) -> Optional[_Extract]:
    host, seg = _host(url), _segments(url)
    if not host.endswith("lever.co") or len(seg) < 2:
        return None
    api = "api.eu.lever.co" if ".eu." in host else "api.lever.co"
    status, d = f.get_json(f"https://{api}/v0/postings/{quote(seg[0])}/{quote(seg[1])}")
    if status == 404:
        return _Extract(source="lever", status="dead")
    if status != 200 or not isinstance(d, dict):
        return _Extract(source="lever", status="error")
    cats = d.get("categories") if isinstance(d.get("categories"), dict) else {}
    lists = "\n".join(f"{x.get('text', '')}\n{html_to_text(x.get('content'))}"
                      for x in d.get("lists") or [] if isinstance(x, dict))
    locs = [cats.get("location"), *(cats.get("allLocations") or [])] if _str(cats.get("location")) else []
    return _Extract(
        source="lever",
        title=_str(d.get("text")),
        locations=[x for x in dict.fromkeys(locs) if _str(x)],
        employment_type=_str(cats.get("commitment")),
        text="\n".join(x for x in (d.get("descriptionPlain"), lists, d.get("additionalPlain")) if _str(x)),
    )


def ashby(url: str, f: SafeFetcher) -> Optional[_Extract]:
    host, seg = _host(url), _segments(url)
    if host != "jobs.ashbyhq.com" or len(seg) < 2:
        return None
    status, d = f.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(seg[0])}")
    if status != 200 or not isinstance(d, dict):
        return _Extract(source="ashby", status="error")  # also a board over the size cap
    for j in d.get("jobs") or []:
        if isinstance(j, dict) and j.get("id") == seg[1]:
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
            country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
            return _Extract(
                source="ashby",
                title=_str(j.get("title")),
                locations=[x for x in [*locs, country] if _str(x)],
                employment_type=_str(j.get("employmentType")),
                text=_str(j.get("descriptionPlain")) or html_to_text(j.get("descriptionHtml")),
            )
    return _Extract(source="ashby", status="dead")  # not on the public board: closed or unlisted


_WD_HOST = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
_LANG = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def workday(url: str, f: SafeFetcher) -> Optional[_Extract]:
    host, seg = _host(url), _segments(url)
    m = _WD_HOST.match(host)
    if not m:
        return None
    if seg and _LANG.match(seg[0]):
        seg = seg[1:]
    if len(seg) < 3 or "job" not in seg:
        return None
    rest = seg[seg.index("job"):]
    status, d = f.get_json(f"https://{host}/wday/cxs/{m.group(1)}/{quote(seg[0])}/" + "/".join(quote(s) for s in rest))
    if status == 404:
        return _Extract(source="workday", status="dead")
    if status != 200 or not isinstance(d, dict) or not isinstance(d.get("jobPostingInfo"), dict):
        return _Extract(source="workday", status="error")
    info = d["jobPostingInfo"]
    locs = [info.get("location"), *(info.get("additionalLocations") or [])]
    country = info.get("country") if isinstance(info.get("country"), dict) else {}
    org = d.get("hiringOrganization") if isinstance(d.get("hiringOrganization"), dict) else {}
    return _Extract(
        source="workday",
        title=_str(info.get("title")),
        company=_str(org.get("name")),
        locations=[x for x in [*locs, country.get("descriptor")] if _str(x)],
        employment_type=_str(info.get("timeType")),  # "Full time" even for interns: never read for level
        text=html_to_text(info.get("jobDescription")),
    )


API_EXTRACTORS = (workday, greenhouse, lever, ashby)

# Company career sites that embed an ATS board: the page links or iframes it.
_EMBED = [
    (re.compile(r"boards(?:-api)?(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=|v1/boards/)([\w-]+)[^\"']*?(?:token=|/jobs/)(\d+)"),
     lambda m: f"https://job-boards.greenhouse.io/{m.group(1)}/jobs/{m.group(2)}"),
    (re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)/([0-9a-f-]{36})"), lambda m: f"https://jobs.lever.co/{m.group(1)}/{m.group(2)}"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})"), lambda m: f"https://jobs.ashbyhq.com/{m.group(1)}/{m.group(2)}"),
]
_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f-]{27}", re.I)
_GH_BOARD = re.compile(r"greenhouse\.io/(?:v1/boards|embed/job_board/js\?for=|embed/job_app\?for=)/?([\w-]+)")


def _redirected_away(url: str, final: str) -> bool:
    """A posting URL that redirects to a page without the job's id: closed, sent to the listing."""
    ids = _ID.findall(urlsplit(url).path + "?" + urlsplit(url).query)
    return bool(ids) and final != url and not any(i in final for i in ids)


def _api_for(url: str, f: SafeFetcher) -> Optional[_Extract]:
    for extractor in API_EXTRACTORS:
        try:
            got = extractor(url, f)
        except Exception as e:  # network errors, and JSON of an unexpected shape
            got = _Extract(source=extractor.__name__, status="error")
            log.info("job API %s: %s", extractor.__name__, type(e).__name__)
        if got is not None:
            return got
    return None


def _from_page(url: str, final: str, status: int, page: str, f: SafeFetcher) -> _Extract:
    """The posting from an already-fetched page: an embedded board, JSON-LD, app state, visible text."""
    if status in (404, 410):
        return _Extract(source="html", status="dead")
    if status != 200:
        return _Extract(source="html", status="error")
    if _redirected_away(url, final):
        return _Extract(source="html", status="dead")
    gh_jid = parse_qs(urlsplit(final).query).get("gh_jid")
    board = _GH_BOARD.search(page)
    embedded = None
    if gh_jid and board:
        embedded = f"https://job-boards.greenhouse.io/{board.group(1)}/jobs/{gh_jid[0]}"
    else:
        for rx, to_url in _EMBED:
            m = rx.search(page)
            if m and _host(to_url(m)) != _host(final):
                embedded = to_url(m)
                break
    if embedded is not None:
        got = _api_for(embedded, f)
        if got is not None and got.has_description:
            return got
    ld = posting_from_jsonld(page)
    if ld is not None and len(ld.text) >= MIN_DESCRIPTION:
        return ld
    app = posting_from_app_json(page)
    if app is not None and len(app.text) >= MIN_DESCRIPTION:
        return app
    p = posting_from_html(page)
    if ld is not None:  # JSON-LD without a usable description: keep its fields, use the page text
        ld.source, ld.status, ld.text, ld.is_event_page = "jsonld+html", p.status, p.text, p.is_event_page
        return ld
    return p


_AMAZON_LOCATION = re.compile(r"""aria-label=["']location["'][^>]*>\s*</span>\s*<ul\b[^>]*>(.{0,2000}?)</ul>""", re.I | re.S)


def amazon_locations(page: str) -> list[str]:
    """amazon.jobs pages list the location(s) in the "Job details" sidebar, e.g. "CAN, ON, Toronto"."""
    m = _AMAZON_LOCATION.search(page)
    if m is None:
        return []
    items = (html_to_text(li).strip() for li in re.findall(r"<li\b[^>]*>(.*?)</li>", m.group(1), re.I | re.S))
    return [x for x in dict.fromkeys(items) if x][:20]


def _status(ex: Optional[_Extract]) -> str:
    if ex is None:
        return "failed"
    if ex.status == "dead":
        return "closed"
    return "ok" if ex.has_description else "failed"


# Wall-clock cap on one link's lookup (all its requests and the parsing). The per-request timeout
# only bounds each socket operation, so a server dribbling bytes could otherwise hold a cycle, and
# every story in it, for hours.
LOOKUP_BUDGET = 30.0
MAX_STUCK = 4  # lookups left running past the budget; beyond this, links aren't looked up


class JobTitles:
    """Looks up the posting behind a job link. At most two pages per link (the page, plus an ATS
    API or an embedded board), fetched only for items with a job link in a cycle that has new
    items. Results are cached per URL until ``start_cycle()``, so stories sharing a link in one
    cycle cost one lookup and a failed lookup is retried in the next cycle.

    Each lookup runs in a daemon thread and is given up after ``budget`` seconds (it then gives the
    slug title only); a lookup still running from an earlier cycle is not started again."""

    def __init__(self, timeout: float = 10, resolve: Callable[..., list] = socket.getaddrinfo,
                 transport: Transport = _pinned_send, budget: Optional[float] = LOOKUP_BUDGET):
        self._fetch = SafeFetcher(timeout, resolve, transport)
        self._cache: dict[str, JobPosting] = {}
        self.budget = budget
        self._stuck: dict[str, threading.Thread] = {}

    def start_cycle(self) -> None:
        self._cache.clear()
        self._stuck = {u: t for u, t in self._stuck.items() if t.is_alive()}

    def lookup(self, url: str) -> JobPosting:
        """The posting for ``url``; never raises. Title and company fall back to page metadata and
        the URL slug when the text can't be read."""
        if url in self._cache:
            return self._cache[url]
        posting = self._bounded(url)
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[url] = posting
        return posting

    def _bounded(self, url: str) -> JobPosting:
        if self.budget is None:
            return self._lookup(url)
        stuck = self._stuck.get(url)
        if (stuck is not None and stuck.is_alive()) or sum(t.is_alive() for t in self._stuck.values()) >= MAX_STUCK:
            return JobPosting(title=_slug_title(url))
        box: list[JobPosting] = []
        worker = threading.Thread(target=lambda: box.append(self._lookup(url)), name="job-lookup", daemon=True)
        worker.start()
        worker.join(self.budget)
        if box:
            return box[0]
        self._stuck[url] = worker
        log.info("job page %s: over the %.0f s budget", _safe_host(url), self.budget)
        return JobPosting(title=_slug_title(url))

    def _lookup(self, url: str) -> JobPosting:
        try:
            return self._read(url)
        except Exception as e:  # never raise into the watcher: a story must still be posted
            log.info("job page %s: %s", _safe_host(url), type(e).__name__)
            return JobPosting(title=_slug_title(url))

    def _read(self, url: str) -> JobPosting:
        title = company = None
        ex: Optional[_Extract] = None
        try:
            api = _api_for(url, self._fetch)
            if api is not None and api.has_description:
                ex = api
            page_url = url
            if (urlsplit(url).hostname or "").endswith(".icims.com"):
                # iCIMS renders the posting in an iframe; ?in_iframe=1 is that iframe's document.
                page_url = url.split("?", 1)[0] + "?in_iframe=1"
            status, final, page = self._fetch.get(page_url)
            if status == 200:
                title, company = title_from_html(page), company_from_html(page)
            if ex is None:
                from_page = _from_page(page_url, final, status, page, self._fetch)
                # An ATS API 404 says closed even when the page still renders a shell.
                ex = api if api is not None and api.status == "dead" and not from_page.has_description else from_page
            if status == 200 and ex is not None and not ex.locations and _host(final).endswith("amazon.jobs"):
                ex.locations = amazon_locations(page)
        except (requests.RequestException, ValueError, OSError, UnicodeError, RecursionError) as e:
            log.info("job page %s: %s", _safe_host(url), type(e).__name__)
        return JobPosting(
            title=title or (ex.title if ex else None) or title_from_slug(url),
            company=company or (clean_company(ex.company) if ex else None),
            status=_status(ex),
            source=ex.source if ex else "none",
            locations=tuple(str(x)[:200] for x in (ex.locations if ex else [])[:20]),
            employment_type=ex.employment_type if ex else None,
            description=(ex.text if ex else "")[:MAX_DESCRIPTION],
            is_event_page=bool(ex and ex.is_event_page),
        )


def _safe_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _slug_title(url: str) -> Optional[str]:
    try:
        return title_from_slug(url)
    except Exception:
        return None


def add_job_info(item: StoryItem, jobs: JobTitles) -> StoryItem:
    """Fill job_title, company and the posting from the item's first job link. A story whose links
    are none of them job links still gets the first https link's page as its posting (often an
    event page), because the tagger was scored with it (#10); the embed keeps "New story" then.
    Never raises: the item is returned unchanged instead."""
    try:
        return _add_job_info(item, jobs)
    except Exception as e:
        log.warning("job info for %s skipped (%s)", item.media_id, type(e).__name__)
        return item


def _add_job_info(item: StoryItem, jobs: JobTitles) -> StoryItem:
    link = next((u for u in item.links if is_job_link(u)), None)
    if link is None:
        other = next((u for u in item.links if u.startswith("https://")), None)
        if other is None:
            return item
        info = jobs.lookup(other)
        return replace(item, posting=info) if isinstance(info, JobPosting) else item
    info = jobs.lookup(link)
    return replace(item, job_title=info.title, company=info.company,
                   posting=info if isinstance(info, JobPosting) else None)
