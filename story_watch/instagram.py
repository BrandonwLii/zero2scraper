"""Thin wrapper around instaloader: session handling, userid cache, story fetch."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import instaloader
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

    @property
    def link(self) -> str:
        return f"https://www.instagram.com/stories/{self.target}/"


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
    ):
        self.ig_user = ig_user
        self.session_path = session_path
        self._loader = loader if loader is not None else _new_loader()
        self._session_loaded = False
        # Known ids skip Profile.from_username (web_profile_info), which 429s easily.
        self._userids: dict[str, int] = dict(userids or {})

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
