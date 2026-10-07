"""Discord webhook sender with retry on 429 and 5xx."""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from .instagram import StoryItem

log = logging.getLogger(__name__)

COLOR_STORY = 0xE1306C
COLOR_ALERT = 0xED4245
COLOR_OK = 0x57F287
COLOR_INFO = 0x5865F2


class DiscordError(Exception):
    """Delivery failed. Messages never include the webhook URL."""


class Notifier:
    def __init__(
        self,
        webhook_url: str,
        session: requests.Session | None = None,
        max_attempts: int = 5,
        timeout: float = 15,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._url = webhook_url
        self._http = session or requests.Session()
        self._max_attempts = max_attempts
        self._timeout = timeout
        self._sleep = sleep

    def __repr__(self) -> str:
        return "Notifier(<webhook redacted>)"

    def send(self, payload: dict) -> None:
        """POST payload; return on 2xx, raise DiscordError otherwise."""
        payload = {"allowed_mentions": {"parse": []}, **payload}
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._http.post(
                    self._url, json=payload, params={"wait": "true"}, timeout=self._timeout
                )
            except requests.RequestException as e:
                # requests' messages embed the URL (and so the token): log the type only.
                log.warning("discord network error (%s), attempt %d", type(e).__name__, attempt)
                if attempt == self._max_attempts:
                    raise DiscordError(f"network error: {type(e).__name__}") from None
                self._sleep(min(2**attempt, 60))
                continue

            status = resp.status_code
            if 200 <= status < 300:
                return
            if status == 429:
                delay = _retry_after(resp)
                log.warning("discord rate limited, retrying in %.2fs", delay)
                if attempt == self._max_attempts:
                    break
                self._sleep(delay)
                continue
            if status >= 500:
                log.warning("discord %d, attempt %d", status, attempt)
                if attempt == self._max_attempts:
                    break
                self._sleep(min(2**attempt, 60))
                continue
            raise DiscordError(f"discord rejected message: HTTP {status}")
        raise DiscordError(f"discord delivery failed after {self._max_attempts} attempts")

    def story(self, item: StoryItem) -> None:
        ts = int(item.taken_at.timestamp())
        self.send(
            {
                "embeds": [
                    {
                        "title": f"New story from @{item.target}",
                        "url": item.link,
                        "color": COLOR_STORY,
                        "fields": [
                            {"name": "Type", "value": "video" if item.is_video else "photo", "inline": True},
                            # <t:..> renders in the viewer's local time zone
                            {"name": "Posted", "value": f"<t:{ts}:f> (<t:{ts}:R>)", "inline": True},
                        ],
                        "image": {"url": item.thumbnail_url},
                        "timestamp": item.taken_at.isoformat(),
                    }
                ]
            }
        )

    def alert(self, title: str, description: str, color: int = COLOR_ALERT) -> None:
        self.send({"embeds": [{"title": title, "description": description[:4000], "color": color}]})


def _retry_after(resp: requests.Response) -> float:
    try:
        value = float(resp.json().get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        try:
            value = float(resp.headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            value = 1.0
    return max(0.0, min(value, 300.0))
