"""Keep a copy of every new story (media + JSON sidecar) so taggers can be evaluated later.

Stories vanish after 24 h and their CDN URLs expire, so the copy is made in the cycle that
first sees the item. Archiving is best effort: save() never raises, and the watcher calls it
only after a target's notifications have been handled (delivered, filtered or failed), so a slow
CDN can't delay a post. Downloads use a fresh requests session, never the
instaloader one, so Instagram cookies can't reach the CDN. Like jobs.py, they must be https,
resolve to public addresses, don't follow redirects, and are size-capped. CDN URLs are signed,
so they are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests

from .classify import legacy_category
from .tags import Tags
from .instagram import StoryItem
from .jobs import USER_AGENT, _check_public

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 25_000_000
MAX_VIDEO_BYTES = 50_000_000
DEADLINE = 90.0  # seconds of download time per item, so a slow CDN can't stall the cycle
DEFAULT_MAX_MB = 2048
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


class DownloadError(Exception):
    pass


def video_url(node: Mapping[str, Any] | None) -> str | None:
    """Best video source in a GraphQL story node: the largest video_resources entry."""
    if not node:
        return None
    resources = node.get("video_resources") or []
    for res in reversed(resources):
        src = (res or {}).get("src")
        if isinstance(src, str) and src:
            return src
    url = node.get("video_url")
    return url if isinstance(url, str) and url else None


def _stem(item: StoryItem) -> str:
    return f"{item.taken_at.strftime('%Y%m%dT%H%M%SZ')}_{item.media_id}"


class Archive:
    def __init__(
        self,
        root: Path,
        max_mb: int = DEFAULT_MAX_MB,
        session: requests.Session | None = None,
        timeout: float = 20,
        resolve: Callable[..., list] = socket.getaddrinfo,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        max_video_bytes: int = MAX_VIDEO_BYTES,
    ):
        self.root = Path(root)
        self.max_bytes = max_mb * 1_000_000
        self._http = session or requests.Session()  # never the instaloader session
        self._http.headers.update({"User-Agent": USER_AGENT})
        self._timeout = timeout
        self._resolve = resolve
        self.max_image_bytes = max_image_bytes
        self.max_video_bytes = max_video_bytes

    # -- download -----------------------------------------------------------------------

    def _download(self, url: str, dest: Path, cap: int, deadline: float) -> None:
        """Stream `url` to `dest` (via a .part file). Raises DownloadError past `cap` bytes."""
        try:
            _check_public(url, self._resolve)
        except ValueError as e:
            raise DownloadError(str(e)) from None
        self._http.cookies.clear()
        part = dest.with_name(dest.name + ".part")
        try:
            resp = self._http.get(url, timeout=self._timeout, allow_redirects=False, stream=True)
            try:
                if resp.status_code != 200:
                    raise DownloadError(f"HTTP {resp.status_code}")
                length = resp.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > cap:
                    raise DownloadError("over size cap")
                size = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(65536):
                        size += len(chunk)
                        if size > cap:
                            raise DownloadError("over size cap")
                        if time.monotonic() > deadline:
                            raise DownloadError("deadline")
                        fh.write(chunk)
            finally:
                resp.close()
            os.replace(part, dest)
        except requests.RequestException as e:
            raise DownloadError(type(e).__name__) from None
        finally:
            part.unlink(missing_ok=True)

    def _fetch_media(self, item: StoryItem, base: Path) -> tuple[list[str], str | None]:
        """Download what we can; returns (saved file names, error type or None)."""
        deadline = time.monotonic() + DEADLINE
        saved: list[str] = []
        error: str | None = None
        wanted: list[tuple[str, str, int]] = []  # (url, extension, cap)
        if item.is_video:
            url = video_url(item.node)
            if url:
                wanted.append((url, ".mp4", self.max_video_bytes))
        ext = Path(urlsplit(item.thumbnail_url).path).suffix.lower()
        wanted.append((item.thumbnail_url, ext if ext in _IMAGE_EXTS else ".jpg", self.max_image_bytes))
        for n, (url, ext, cap) in enumerate(wanted):
            dest = base.with_suffix(ext)
            if dest.exists():
                saved.append(dest.name)
            else:
                try:
                    self._download(url, dest, cap, deadline)
                    saved.append(dest.name)
                except DownloadError as e:
                    error = error or str(e)
                    log.warning("archive %s: %s download failed (%s)", item.media_id, ext, e)
                    continue
                except Exception as e:
                    error = error or type(e).__name__
                    log.warning("archive %s: %s download failed (%s)", item.media_id, ext, type(e).__name__)
                    continue
            if ext == ".mp4":
                break  # a poster image is only fetched when the video is missing
        return saved, error

    # -- sidecar ------------------------------------------------------------------------

    @staticmethod
    def _sidecar(item: StoryItem, tags: Tags, classifier: str, files: list[str], error: str | None) -> dict:
        return {
            "media_id": item.media_id,
            "target": item.target,
            "taken_at": item.taken_at.isoformat(),
            "is_video": item.is_video,
            "links": list(item.links),
            "mentions": list(item.mentions),
            "job_title": item.job_title,
            "company": item.company,
            "classifier": classifier,
            "tags": tags.to_dict(),
            "category": legacy_category(tags),  # pre-tags field, kept for older readers
            "files": files,
            "download_error": error,
            "node": item.node,
            "page_node": item.page_node,
        }

    # -- retention ----------------------------------------------------------------------

    def prune(self, keep: str | None = None) -> int:
        """Delete the oldest items until the archive fits `max_bytes`. Returns items removed.

        Items are the files sharing a name stem (timestamp first, so name order is age).
        The item named `keep` is never removed.
        """
        groups: dict[Path, list[Path]] = {}
        for f in self.root.glob("*/*"):
            if f.is_file():
                groups.setdefault(f.with_suffix(""), []).append(f)
        total = sum(f.stat().st_size for files in groups.values() for f in files)
        removed = 0
        for stem in sorted(groups, key=lambda p: p.name):
            if total <= self.max_bytes:
                break
            if stem.name == keep:
                continue
            for f in groups[stem]:
                total -= f.stat().st_size
                f.unlink(missing_ok=True)
            removed += 1
        return removed

    # -- entry point --------------------------------------------------------------------

    def save(self, item: StoryItem, tags: Tags, classifier: str = "") -> bool:
        """Archive `item`. Never raises; returns whether the sidecar was written."""
        try:
            folder = self.root / item.target
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            base = folder / _stem(item)
            files, error = self._fetch_media(item, base)
            doc = self._sidecar(item, tags, classifier, files, error)
            tmp = base.with_suffix(".json.part")
            tmp.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str), encoding="utf-8")
            os.replace(tmp, base.with_suffix(".json"))
            removed = self.prune(keep=base.name)
            if removed:
                log.info("archive over size cap: removed %d oldest item(s)", removed)
            log.info("archived %s item %s (%d file(s))", item.target, item.media_id, len(files))
            return True
        except Exception as e:
            log.warning("archive failed for %s (%s)", item.media_id, type(e).__name__)
            return False
