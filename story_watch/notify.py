"""Discord webhook sender with retry on 429 and 5xx."""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit

import requests

from .tags import DIMENSIONS, Tags
from .instagram import StoryItem
from .jobs import is_job_link

log = logging.getLogger(__name__)

COLOR_STORY = 0xE1306C
COLOR_ALERT = 0xED4245
COLOR_OK = 0x57F287
COLOR_INFO = 0x5865F2


# Job boards whose first path segment is the company, so it's worth showing.
_COMPANY_IN_PATH = ("job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com")


def link_label(url: str) -> str:
    """Short human label for a link: host, plus the company slug on known job boards."""
    parts = urlsplit(url)
    host = (parts.hostname or url).removeprefix("www.")
    segments = [s for s in parts.path.split("/") if s]
    if host in _COMPANY_IN_PATH and segments:
        return f"{host}/{segments[0]}"
    return host


def _join_within(lines: list[str], limit: int = 1024) -> str:
    """Join whole lines up to Discord's field limit; never cut a URL in half."""
    out: list[str] = []
    for line in lines:
        if len("\n".join(out + [line])) > limit:
            break
        out.append(line)
    return "\n".join(out) or lines[0][: limit - 1] + "…"


MAX_CONTENT = 2000  # Discord's limit for a message's content
MAX_MENTIONS = 100  # Discord's limit for allowed_mentions.users


def split_mentions(user_ids: Iterable[str], first_budget: int) -> list[list[str]]:
    """Group user ids so each group's mentions (`<@id>` joined by spaces) fit both Discord
    limits. The first group must fit in `first_budget` characters (the rest of the main
    message's content); the others get a whole message. Anything that isn't a 15-21 digit
    id is dropped and duplicates collapsed, so nothing but a user mention can end up in
    `content`."""
    ids = dict.fromkeys(u for u in user_ids if isinstance(u, str) and u.isascii() and u.isdigit() and 15 <= len(u) <= 21)
    groups: list[list[str]] = []
    used, budget = 0, first_budget
    for uid in ids:
        cost = len(uid) + 3 + (1 if groups and groups[-1] else 0)  # "<@" + id + ">", plus a space
        if groups and (len(groups[-1]) >= MAX_MENTIONS or used + cost > budget):
            groups.append([])
            used, budget, cost = 0, MAX_CONTENT, len(uid) + 3
        elif not groups:
            groups.append([])
            cost = len(uid) + 3
        groups[-1].append(uid)
        used += cost
    return groups


class DiscordError(Exception):
    """Delivery failed. Messages never include the webhook URL."""


def tags_text(tags: Tags) -> str:
    """One line for the embed footer: a certain dimension shows its value, several values
    are joined with "/", no idea shows "?", and dimensions that don't apply are left out."""
    parts = []
    for name, cls in DIMENSIONS.items():
        values = tags.values(name)
        if values is None:
            continue
        shown = "?" if tags.is_unsure(name) else "/".join(m.label for m in cls if m in values)
        parts.append(shown if name == "post_type" else f"{name.replace('_', ' ').capitalize()}: {shown}")
    return " · ".join(parts)


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

    def send(self, payload: dict, max_attempts: int | None = None) -> None:
        """POST payload; return on 2xx, raise DiscordError otherwise."""
        payload = {"allowed_mentions": {"parse": []}, **payload}
        max_attempts = max_attempts or self._max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self._http.post(
                    self._url, json=payload, params={"wait": "true"}, timeout=self._timeout
                )
            except requests.RequestException as e:
                # requests' messages embed the URL (and so the token): log the type only.
                log.warning("discord network error (%s), attempt %d", type(e).__name__, attempt)
                if attempt == max_attempts:
                    raise DiscordError(f"network error: {type(e).__name__}") from None
                self._sleep(min(2**attempt, 60))
                continue

            status = resp.status_code
            if 200 <= status < 300:
                return
            if status == 429:
                delay = _retry_after(resp)
                log.warning("discord rate limited, retrying in %.2fs", delay)
                if attempt == max_attempts:
                    break
                self._sleep(delay)
                continue
            if status >= 500:
                log.warning("discord %d, attempt %d", status, attempt)
                if attempt == max_attempts:
                    break
                self._sleep(min(2**attempt, 60))
                continue
            raise DiscordError(f"discord rejected message: HTTP {status}")
        raise DiscordError(f"discord delivery failed after {max_attempts} attempts")

    def story(self, item: StoryItem, tags: Tags | None = None, user_ids: Iterable[str] = ()) -> None:
        """Post one story, pinging `user_ids` (Discord user IDs; choosing them is the caller's job).

        Returns once the main post is accepted. Mentions that don't fit in it go in follow-up
        mention-only messages, which are best effort: a failed one is logged and dropped.
        """
        ts = int(item.taken_at.timestamp())
        fields = [
            # <t:..> renders in the viewer's local time zone
            {"name": "Posted", "value": f"<t:{ts}:f> (<t:{ts}:R>)", "inline": True},
        ]
        links = [u for u in item.links if u.startswith(("https://", "http://"))]
        job_link = next((u for u in links if is_job_link(u)), None)
        if job_link and item.job_title:
            # Company first: Discord uses the title as the push-notification text.
            company = item.company
            if company and not item.job_title.lower().startswith(company.lower()):
                title = f"{company}: {item.job_title}"
            else:
                title = item.job_title
            url = job_link
        elif job_link and item.company:
            title, url = f"{item.company}: job posting", job_link
        elif job_link:
            title, url = f"@{item.target}: {link_label(job_link)}", job_link
        else:
            title, url = f"New story from @{item.target}", item.link
        if links:
            value = _join_within([f"[{link_label(u)}]({u})" if len(u) < 400 else u for u in links])
            fields.append({"name": "Link" if len(links) == 1 else "Links", "value": value})
        if item.mentions:
            fields.append({"name": "Mentions", "value": _join_within([", ".join(f"@{m}" for m in item.mentions)])})
        if job_link:
            fields.append({"name": "Story", "value": f"[Open on Instagram]({item.link})"})
        embed = {
            "title": title[:256],
            "url": url,
            "color": COLOR_STORY,
            "fields": fields,
            "image": {"url": item.thumbnail_url},
            "timestamp": item.taken_at.isoformat(),
        }
        if job_link:
            embed["author"] = {"name": f"New story from @{item.target}"}
        if tags is not None:
            embed["footer"] = {"text": tags_text(tags)}
        payload: dict = {"embeds": [embed]}
        content = title[:256]
        groups = split_mentions(user_ids, MAX_CONTENT - len(content) - 1)
        if groups:
            # Content (not the embed) is what a ping notifies with, so repeat the title
            # there, mentions last. Only these users may be pinged, never roles or everyone.
            payload["content"] = f"{content} {_mentions(groups[0])}"
            payload["allowed_mentions"] = {"parse": [], "users": groups[0]}
        self.send(payload)
        for group in groups[1:]:
            try:
                self.send({"content": _mentions(group), "allowed_mentions": {"parse": [], "users": group}},
                          max_attempts=2)
            except DiscordError as e:
                log.warning("follow-up mentions not sent: %s", e)

    def alert(self, title: str, description: str, color: int = COLOR_ALERT) -> None:
        self.send({"embeds": [{"title": title, "description": description[:4000], "color": color}]})


def _mentions(ids: list[str]) -> str:
    return " ".join(f"<@{u}>" for u in ids)


def _retry_after(resp: requests.Response) -> float:
    try:
        value = float(resp.json().get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        try:
            value = float(resp.headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            value = 1.0
    return max(0.0, min(value, 300.0))
