"""Turn a story's link sticker into a job title by reading the job page's metadata.

Order: JSON-LD JobPosting.title, then og:title, then <title>, then a title-ish
URL slug. Pages come from arbitrary third-party sites, so requests never carry
Instagram cookies, must be https, must resolve to public addresses (the service
runs inside a home network), and are size-capped.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
from dataclasses import replace
from typing import Any, Callable, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

import requests

from .instagram import StoryItem

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
MAX_TITLE = 200

_JOB_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "icims.com", "smartrecruiters.com",
              "workable.com", "oraclecloud.com", "jobvite.com", "bamboohr.com", "recruitee.com")
_JOB_WORDS = re.compile(r"(^|[/._?&=-])(jobs?|careers?|gh_jid|jobid|job-detail|positions?|openings?)([/._?&=-]|$)", re.I)

_LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_OG_TITLE = re.compile(
    r"""<meta[^>]+(?:property|name)=["']og:title["'][^>]*content=["']([^"']*)["']"""
    r"""|<meta[^>]+content=["']([^"']*)["'][^>]*(?:property|name)=["']og:title["']""",
    re.I,
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_GREENHOUSE_TITLE = re.compile(r"^Job Application for (.+) at .+$")
# og:title/<title> of a careers landing page rather than a posting (JSON-LD is trusted).
_GENERIC_PAGE = re.compile(r"\bcareers?\b|\bcareer site\b|\bjob search\b|^jobs?$", re.I)
_HOSTED_AT = re.compile(r"^(?:Job Application for )?.+ (?:at|@) (.+)$")  # Greenhouse "at X", Ashby "@ X"
_SITE_NAME = re.compile(
    r"""<meta[^>]+property=["']og:site_name["'][^>]*content=["']([^"']*)["']"""
    r"""|<meta[^>]+content=["']([^"']*)["'][^>]*property=["']og:site_name["']""",
    re.I,
)
_ORG_CODE = re.compile(r"^(?:[A-Z]*\d[A-Z\d]*\s+)+")  # Workday entity codes: "2100 NVIDIA USA", "IF1690 ..."
_ORG_SUFFIX = re.compile(
    r"(?:,?\s+(?:U\.?S\.?A?\.?|LLC|L\.?P\.?|Inc\.?|Corp\.?|Corporation|Ltd\.?|Careers?|Jobs))+$|\.jobs$", re.I
)
# hiringOrganization values that name a hiring programme, not the company (Citi: "Early Career").
_NOT_A_COMPANY = re.compile(r"career|universit|campus|student|graduate|talent|recruit|intern", re.I)
_MAX_COMPANY = 60


class JobInfo(NamedTuple):
    title: str | None
    company: str | None


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


def _ld_postings(page: str):
    for block in _LD_JSON.findall(page):
        try:
            posting = _ld_job_posting(json.loads(block.strip()))
        except ValueError:
            continue
        if posting:
            yield posting


def _page_title(page: str) -> str | None:
    tag = _TITLE.search(page)
    return _clean(tag.group(1)) if tag else None


def title_from_html(page: str) -> str | None:
    for posting in _ld_postings(page):
        title = _clean(posting.get("title") if isinstance(posting.get("title"), str) else None)
        if title:
            return title
    candidates = []
    og = _OG_TITLE.search(page)
    if og:
        candidates.append(_clean(og.group(1) or og.group(2)))
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
    site = _SITE_NAME.search(page)
    if site:
        candidates.append(site.group(1) or site.group(2))
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


def _check_public(url: str, resolve: Callable[..., list]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("not an https URL")
    for info in resolve(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("resolves to a non-public address")


class JobTitles:
    def __init__(self, session: requests.Session | None = None, timeout: float = 10,
                 resolve: Callable[..., list] = socket.getaddrinfo):
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"})
        self._timeout = timeout
        self._resolve = resolve
        self._cache: dict[str, JobInfo] = {}

    def _fetch(self, url: str) -> str:
        for _ in range(MAX_REDIRECTS + 1):
            _check_public(url, self._resolve)
            resp = self._http.get(url, timeout=self._timeout, allow_redirects=False, stream=True)
            try:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("Location", ""))
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                body = b""
                for chunk in resp.iter_content(65536):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                return body.decode(resp.encoding or "utf-8", "replace")
            finally:
                resp.close()
        raise ValueError("too many redirects")

    def lookup(self, url: str) -> JobInfo:
        """Job title and company for `url` (either may be None). Never raises; cached per URL."""
        if url in self._cache:
            return self._cache[url]
        info = JobInfo(None, None)
        try:
            page = self._fetch(url)
            info = JobInfo(title_from_html(page), company_from_html(page))
        except (requests.RequestException, ValueError, OSError, UnicodeError) as e:
            log.info("job page %s: %s", urlsplit(url).hostname, type(e).__name__)
        info = info._replace(title=info.title or title_from_slug(url))
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[url] = info
        return info


def add_job_info(item: StoryItem, jobs: JobTitles) -> StoryItem:
    """Fill job_title/company from the item's first job link, if it has one."""
    link = next((u for u in item.links if is_job_link(u)), None)
    if link is None:
        return item
    info = jobs.lookup(link)
    return replace(item, job_title=info.title, company=info.company)
