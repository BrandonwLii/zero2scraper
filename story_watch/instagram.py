"""Thin wrapper around instaloader: session handling, userid cache, story fetch."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

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
    def __init__(self, ig_user: str, session_path: str | None = None, loader=None):
        self.ig_user = ig_user
        self.session_path = session_path
        self._loader = loader if loader is not None else _new_loader()
        self._session_loaded = False
        self._userids: dict[str, int] = {}

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
                for it in story.get_items():
                    items.append(
                        StoryItem(
                            media_id=str(it.mediaid),
                            target=target,
                            taken_at=it.date_utc.replace(tzinfo=timezone.utc),
                            is_video=bool(it.is_video),
                            thumbnail_url=it.url,
                        )
                    )
            return items
        except (ie.LoginRequiredException, ie.LoginException) as e:
            self._session_loaded = False  # reload from disk next time (after re-login)
            raise SessionError(f"{type(e).__name__}: {e}") from e
        except ie.InstaloaderException as e:
            if any(m in str(e).lower() for m in _SESSION_MARKERS):
                self._session_loaded = False
                raise SessionError(f"{type(e).__name__}: {e}") from e
            raise InstagramError(f"{type(e).__name__}: {e}") from e
