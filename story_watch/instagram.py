"""Thin wrapper around instaloader: session handling, userid cache, story fetch."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import instaloader
import requests
from instaloader import exceptions as ie

log = logging.getLogger(__name__)

_SESSION_MARKERS = ("checkpoint", "challenge_required", "login_required")


class InstagramError(Exception):
    """Transient Instagram failure (network, rate limit, odd response)."""


class SessionError(InstagramError):
    """The saved session is missing, expired, or stuck at a checkpoint."""


@dataclass(frozen=True)
class StoryItem:
    media_id: str
    target: str
    taken_at: datetime  # tz-aware UTC
    is_video: bool
    thumbnail_url: str
    links: tuple[str, ...] = ()  # link-sticker targets, unwrapped
    mentions: tuple[str, ...] = ()  # @usernames tagged in the story
    job_title: str | None = None  # from the first job link's page, if any
    company: str | None = None  # hiring company from the same page
    # Raw Instagram JSON for the archive: the GraphQL item and the story-page item (which has
    # the stickers). Left out of == and repr, since they hold signed CDN URLs.
    node: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    page_node: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    # The job posting behind the first job link (jobs.JobPosting), for the tagger. Not archived.
    posting: Any = field(default=None, repr=False, compare=False)

    @property
    def link(self) -> str:
        return f"https://www.instagram.com/stories/{self.target}/"


_REDIRECT_HOSTS = ("l.instagram.com", "l.facebook.com")
_TRACKING_PARAMS = ("fbclid", "igshid")


def unwrap_link(url: str) -> str:
    """Undo Instagram's l.instagram.com/?u=... redirect and drop fbclid-style tracking."""
    parts = urlsplit(url)
    if parts.hostname in _REDIRECT_HOSTS:
        target = parse_qs(parts.query).get("u")
        if target and target[0].startswith(("http://", "https://")):
            parts = urlsplit(target[0])
    query = [(k, v) for k, vs in parse_qs(parts.query, keep_blank_values=True).items() for v in vs]
    kept = [(k, v) for k, v in query if k not in _TRACKING_PARAMS and not k.startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def links_from_item(item: dict[str, Any]) -> tuple[str, ...]:
    """Link-sticker URLs from an api/v1-style story item (story_link_stickers)."""
    links = []
    for sticker in item.get("story_link_stickers") or []:
        url = ((sticker or {}).get("story_link") or {}).get("url")
        if isinstance(url, str) and url:
            clean = unwrap_link(url)
            if clean not in links:
                links.append(clean)
    return tuple(links)


def mentions_from_item(item: dict[str, Any]) -> tuple[str, ...]:
    """@mention usernames from an api/v1-style story item (bloks stickers and reel_mentions)."""
    names = []
    for sticker in item.get("story_bloks_stickers") or []:
        data = (((sticker or {}).get("bloks_sticker") or {}).get("sticker_data") or {})
        names.append((data.get("ig_mention") or {}).get("username"))
    for mention in item.get("reel_mentions") or []:
        names.append(((mention or {}).get("user") or {}).get("username"))
    out = []
    for n in names:
        if isinstance(n, str) and n and n not in out:
            out.append(n)
    return tuple(out)


_SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def _walk_items(obj: Any, found: dict[str, dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        if "pk" in obj and "story_link_stickers" in obj:
            found.setdefault(str(obj["pk"]), obj)
        for v in obj.values():
            _walk_items(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_items(v, found)


def items_from_story_page(html: str) -> dict[str, dict[str, Any]]:
    """api/v1-style story items embedded in a /stories/<user>/ page, keyed by media id."""
    found: dict[str, dict[str, Any]] = {}
    for block in _SCRIPT_JSON.findall(html):
        try:
            _walk_items(json.loads(block), found)
        except ValueError:
            continue
    return found


# Marking stories seen (#50), the way the web client does when it shows a story: the GraphQL
# mutation PolarisStoriesV3SeenMutation (root field xdt_mark_story_reel_seen) on /api/graphql,
# one request per story item. Older tools used the legacy POST /api/v1/stories/reel/seen/ form;
# the 2026 projects that block or replay seen receipts all target this mutation instead.
# The doc_id rotates with Instagram's web bundles: confirm it, the variables and the referer
# from a browser HAR before MARK_SEEN=1 goes to production. Last observed 2026-09-27.
SEEN_URL = "https://www.instagram.com/api/graphql"
SEEN_DOC_ID = "26234228992942885"
SEEN_FRIENDLY_NAME = "PolarisStoriesV3SeenMutation"
SEEN_ROOT_FIELD = "xdt_mark_story_reel_seen"
SEEN_TIMEOUT = 15
DEFAULT_APP_ID = "936619743392459"  # X-IG-App-ID of www.instagram.com (instaloader sends it too)

# Per-page tokens embedded in any logged-in page's HTML (the /stories/<user>/ page we already
# fetch). fb_dtsg is the one Instagram checks. Bounded character classes keep these linear.
_TOKEN_PATTERNS = {
    "fb_dtsg": re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"\\]{1,512})"'),
    "lsd": re.compile(r'"LSD",\[\],\{"token":"([^"\\]{1,512})"'),
    "app_id": re.compile(r'"X-IG-App-ID":"(\d{1,30})"'),
    "spin_r": re.compile(r'"__spin_r":(\d{1,30})'),
    "spin_b": re.compile(r'"__spin_b":"([^"\\]{1,128})"'),
    "spin_t": re.compile(r'"__spin_t":(\d{1,30})'),
    "hsi": re.compile(r'"hsi":"(\d{1,40})"'),
    "haste_session": re.compile(r'"haste_session":"([^"\\]{1,128})"'),
}


@dataclass(frozen=True)
class WebTokens:
    """Tokens from a logged-in page that the web client sends with its GraphQL writes."""

    fb_dtsg: str = field(repr=False)
    lsd: str = field(default="", repr=False)
    app_id: str = DEFAULT_APP_ID
    spin_r: str = ""
    spin_b: str = ""
    spin_t: str = ""
    hsi: str = ""
    haste_session: str = ""


def web_tokens_from_page(html: str) -> WebTokens | None:
    """The page's tokens, or None when it has no fb_dtsg (logged out, or the bundle changed)."""
    found = {}
    for name, pattern in _TOKEN_PATTERNS.items():
        m = pattern.search(html)
        if m:
            found[name] = m.group(1)
    if not found.get("fb_dtsg"):
        return None
    return WebTokens(**found)


@dataclass(frozen=True)
class SeenRequest:
    url: str
    headers: dict[str, str] = field(repr=False)  # holds the CSRF token
    data: dict[str, str] = field(repr=False)  # holds fb_dtsg


def build_seen_request(
    *,
    target: str,
    media_id: str,
    owner_id: int | str,
    taken_at: datetime,
    viewed_at: int,
    viewer_id: str,
    csrftoken: str,
    tokens: WebTokens,
) -> SeenRequest:
    """The POST that marks one story item seen, shaped like the web client's. Pure."""
    variables = {  # the browser's five variables, in its order
        "reelId": str(owner_id),  # a live reel's id is its owner's user id
        "reelMediaId": str(media_id),
        "reelMediaOwnerId": str(owner_id),
        "reelMediaTakenAt": int(taken_at.timestamp()),
        "viewSeenAt": int(viewed_at),
    }
    data = {
        "av": viewer_id,
        "__d": "www",
        "__user": "0",
        "__a": "1",
        "__req": "1",
        "__hs": tokens.haste_session,
        "dpr": "2",
        "__ccg": "EXCELLENT",
        "__rev": tokens.spin_r,
        "__hsi": tokens.hsi,
        "__comet_req": "7",
        "fb_dtsg": tokens.fb_dtsg,
        "jazoest": "2" + str(sum(ord(c) for c in tokens.fb_dtsg)),
        "lsd": tokens.lsd,
        "__spin_r": tokens.spin_r,
        "__spin_b": tokens.spin_b,
        "__spin_t": tokens.spin_t,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": SEEN_FRIENDLY_NAME,
        "server_timestamps": "true",
        "doc_id": SEEN_DOC_ID,
        "variables": json.dumps(variables, separators=(",", ":")),
    }
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.instagram.com",
        "Referer": f"https://www.instagram.com/stories/{target}/{media_id}/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-ASBD-ID": "359341",
        "X-CSRFToken": csrftoken,
        "X-FB-Friendly-Name": SEEN_FRIENDLY_NAME,
        "X-FB-LSD": tokens.lsd,
        "X-IG-App-ID": tokens.app_id,
    }
    return SeenRequest(url=SEEN_URL, headers=headers, data=data)


_LOGIN_PATHS = ("/accounts/login", "/challenge", "/auth_platform")
_JSON_PREFIX = "for (;;);"


def check_seen_response(status: int, location: str, text: str) -> None:
    """Raise SessionError or InstagramError unless the mutation answered with its result.

    Messages hold only fixed text and the HTTP status, never the URL, cookies or the body.
    """
    if 300 <= status < 400:
        if any(p in urlsplit(location or "").path for p in _LOGIN_PATHS):
            raise SessionError("mark seen: redirected to login")
        raise InstagramError(f"mark seen: HTTP {status}")
    body = (text or "").strip()
    if body.startswith(_JSON_PREFIX):
        body = body[len(_JSON_PREFIX):]
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    # Only the reply's own error fields count: an HTML shell's scripts mention these words too.
    if isinstance(reply, dict):
        fields = [reply.get(k) for k in ("message", "error_type", "status")]
        if reply.get("checkpoint_url") or any(
            isinstance(v, str) and any(m in v.lower() for m in _SESSION_MARKERS) for v in fields
        ):
            raise SessionError("mark seen: session needs login or a checkpoint")
    if status != 200:
        raise InstagramError(f"mark seen: HTTP {status}")
    if reply is None:
        raise InstagramError("mark seen: reply is not JSON")
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get(SEEN_ROOT_FIELD), dict):
        raise InstagramError("mark seen: no result in the reply")


def _item_from_node(node: dict[str, Any], target: str) -> StoryItem:
    """Build a StoryItem from a GraphQL reels_media item node."""
    resources = node.get("display_resources") or []
    return StoryItem(
        media_id=str(node["id"]),
        target=target,
        taken_at=datetime.fromtimestamp(node["taken_at_timestamp"], tz=timezone.utc),
        is_video=bool(node.get("is_video")),
        thumbnail_url=resources[-1]["src"] if resources else node["display_url"],
        node=node,
    )


def _new_loader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        max_connection_attempts=1,  # we do our own backoff
    )


class InstagramClient:
    def __init__(
        self,
        ig_user: str,
        session_path: str | None = None,
        loader=None,
        userids: Mapping[str, int] | None = None,
        page_session: requests.Session | None = None,
    ):
        self.ig_user = ig_user
        self.session_path = session_path
        self._loader = loader if loader is not None else _new_loader()
        self._session_loaded = False
        # Known ids skip Profile.from_username (web_profile_info), which 429s easily.
        self._userids: dict[str, int] = dict(userids or {})
        self._page = page_session
        self._tokens: WebTokens | None = None  # from the latest story page, for mark_seen

    def _ensure_session(self) -> None:
        if self._session_loaded:
            return
        try:
            self._loader.load_session_from_file(self.ig_user, self.session_path)
        except FileNotFoundError:
            raise SessionError(
                f"no instaloader session file for {self.ig_user!r}; run instaloader --login"
            ) from None
        self._session_loaded = True
        log.info("loaded Instagram session for %s", self.ig_user)

    def _userid(self, target: str) -> int:
        if target not in self._userids:
            profile = instaloader.Profile.from_username(self._loader.context, target)
            self._userids[target] = profile.userid
            log.info("resolved @%s -> userid %s", target, profile.userid)
        return self._userids[target]

    def fetch_story_items(self, target: str) -> list[StoryItem]:
        self._ensure_session()
        try:
            uid = self._userid(target)
            items = []
            for story in self._loader.get_stories(userids=[uid]):
                # Not story.get_items(): it also calls the iPhone reels_media
                # endpoint, which returns an empty reel for this session and
                # raises KeyError. The GraphQL node has everything we use.
                items.extend(_item_from_node(node, target) for node in story._node["items"])
            return items
        except (KeyError, TypeError) as e:
            raise InstagramError(f"unexpected story response: {type(e).__name__}: {e}") from e
        except (ie.LoginRequiredException, ie.LoginException) as e:
            self._session_loaded = False  # reload from disk next time (after re-login)
            raise SessionError(f"{type(e).__name__}: {e}") from e
        except ie.InstaloaderException as e:
            if any(m in str(e).lower() for m in _SESSION_MARKERS):
                self._session_loaded = False
                raise SessionError(f"{type(e).__name__}: {e}") from e
            raise InstagramError(f"{type(e).__name__}: {e}") from e

    def _page_session(self) -> requests.Session:
        if self._page is None:
            self._page = requests.Session()
            self._page.headers.update(
                {
                    "User-Agent": self._loader.context.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
        # Re-copy every time so a re-login on disk is picked up.
        source = getattr(self._loader.context, "_session", None)
        if source is not None:
            self._page.cookies.update(source.cookies)
        return self._page

    def story_page_items(self, target: str) -> dict[str, dict[str, Any]]:
        """Raw api/v1-style items from the story web page, keyed by media id.

        The GraphQL feed omits stickers; the page embeds them. Plain HTML, no
        JavaScript runs, so this doesn't mark stories seen by itself (mark_seen does, #50).
        Also keeps the page's tokens for mark_seen. Raises InstagramError.
        """
        self._ensure_session()
        try:
            # The first load bounces through ?r=1 to set a cookie; follow it.
            resp = self._page_session().get(f"https://www.instagram.com/stories/{target}/", timeout=30)
        except requests.RequestException as e:
            raise InstagramError(f"story page: {type(e).__name__}") from None
        path = urlsplit(resp.url).path
        if any(p in path for p in ("/accounts/login", "/challenge", "/auth_platform")):
            raise InstagramError("story page redirected to login")
        if resp.status_code != 200:
            raise InstagramError(f"story page: HTTP {resp.status_code}")
        page_items = items_from_story_page(resp.text)
        tokens = web_tokens_from_page(resp.text)
        if tokens is not None:
            self._tokens = tokens
        log.info("@%s: story page has %d item(s)", target, len(page_items))
        return page_items

    def with_extras(self, target: str, items: list[StoryItem]) -> list[StoryItem]:
        """Return `items` with links and mentions from the story web page. Raises InstagramError."""
        return apply_page_items(items, self.story_page_items(target))


    def mark_seen(self, target: str, items: list[StoryItem], now: float | None = None) -> int:
        """Mark `items` (oldest first) of `target`'s reel seen, one mutation each, as the web
        client does while showing them. Uses the session cookies and the tokens of the last
        story page fetched. Stops at the first failure. Returns how many were marked.
        Raises SessionError or InstagramError (messages never hold URLs, cookies or bodies)."""
        tokens = self._tokens
        if tokens is None:
            raise InstagramError("mark seen: no page tokens (story page not fetched)")
        owner_id = self._userids.get(target)
        if owner_id is None:
            raise InstagramError("mark seen: unknown userid")
        session = self._page_session()
        try:
            csrftoken = session.cookies.get("csrftoken") or ""
            viewer_id = session.cookies.get("ds_user_id") or ""
        except Exception as e:  # e.g. requests' CookieConflictError
            raise InstagramError(f"mark seen: cookies unreadable ({type(e).__name__})") from None
        if not csrftoken or not viewer_id:
            raise SessionError("mark seen: no csrftoken or ds_user_id cookie")
        marked = 0
        for item in items:
            req = build_seen_request(
                target=target,
                media_id=item.media_id,
                owner_id=owner_id,
                taken_at=item.taken_at,
                viewed_at=int(time.time() if now is None else now),
                viewer_id=viewer_id,
                csrftoken=csrftoken,
                tokens=tokens,
            )
            # None drops the page fetch's navigation-only header; the others are overridden.
            headers: dict[str, Any] = {**req.headers, "Upgrade-Insecure-Requests": None}
            try:
                resp = session.post(req.url, data=req.data, headers=headers,
                                    timeout=SEEN_TIMEOUT, allow_redirects=False)
            except requests.RequestException as e:
                raise InstagramError(f"mark seen: {type(e).__name__}") from None
            try:
                check_seen_response(resp.status_code, resp.headers.get("Location", ""), resp.text)
            except SessionError:
                self._session_loaded = False  # reload from disk next time (after re-login)
                raise
            marked += 1
        return marked


def apply_page_items(items: list[StoryItem], page_items: Mapping[str, dict[str, Any]]) -> list[StoryItem]:
    out = []
    for item in items:
        raw = page_items.get(item.media_id)
        if raw is not None:
            item = replace(item, links=links_from_item(raw), mentions=mentions_from_item(raw), page_node=raw)
        out.append(item)
    return out
