"""Thin wrapper around instaloader: session handling, userid cache, story fetch."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
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


def _item_from_node(node: dict[str, Any], target: str) -> StoryItem:
    """Build a StoryItem from a GraphQL reels_media item node."""
    resources = node.get("display_resources") or []
    return StoryItem(
        media_id=str(node["id"]),
        target=target,
        taken_at=datetime.fromtimestamp(node["taken_at_timestamp"], tz=timezone.utc),
        is_video=bool(node.get("is_video")),
        thumbnail_url=resources[-1]["src"] if resources else node["display_url"],
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

    def with_extras(self, target: str, items: list[StoryItem]) -> list[StoryItem]:
        """Return `items` with links and mentions from the story web page.

        The GraphQL feed omits stickers; the page embeds them. Plain HTML, no
        JavaScript runs, so this doesn't mark stories seen. Raises InstagramError.
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
        log.info("@%s: story page has %d item(s)", target, len(page_items))
        out = []
        for item in items:
            raw = page_items.get(item.media_id)
            if raw is not None:
                item = replace(item, links=links_from_item(raw), mentions=mentions_from_item(raw))
            out.append(item)
        return out
