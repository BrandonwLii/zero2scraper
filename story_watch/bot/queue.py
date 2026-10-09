"""Which archived stories still need posting in the label channel. No Discord, no network.

The watcher's archive keeps one folder per target holding `<time>_<media id>.json` sidecars and
their media. The bot only reads those; what it writes lives at the archive's top level (see
config.py) because Archive.prune() deletes files in `<archive>/*/*`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

log = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class Sidecar:
    media_id: str
    target: str
    taken_at: datetime
    is_video: bool
    links: tuple[str, ...]
    mentions: tuple[str, ...]
    job_title: str | None
    company: str | None
    classifier: str
    category: str
    files: tuple[str, ...]
    download_error: str | None
    folder: Path

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any], folder: Path) -> "Sidecar":
        taken_at = datetime.fromisoformat(doc["taken_at"])
        if taken_at.tzinfo is None:
            taken_at = taken_at.replace(tzinfo=timezone.utc)
        return cls(
            media_id=str(doc["media_id"]),
            target=str(doc["target"]),
            taken_at=taken_at,
            is_video=bool(doc.get("is_video")),
            links=tuple(str(x) for x in doc.get("links") or ()),
            mentions=tuple(str(x) for x in doc.get("mentions") or ()),
            job_title=doc.get("job_title") or None,
            company=doc.get("company") or None,
            classifier=str(doc.get("classifier") or ""),
            category=str(doc.get("category") or ""),
            files=tuple(str(x) for x in doc.get("files") or ()),
            download_error=doc.get("download_error") or None,
            folder=folder,
        )


def scan_archive(root: Path) -> list[Sidecar]:
    """Every readable sidecar, oldest first. Unreadable or odd files are skipped."""
    found: list[Sidecar] = []
    try:
        folders = sorted(p for p in Path(root).iterdir() if p.is_dir())
    except FileNotFoundError:
        return []
    for folder in folders:
        for path in folder.glob("*.json"):  # in-progress writes end in .json.part
            try:
                found.append(Sidecar.from_doc(json.loads(path.read_text(encoding="utf-8")), folder))
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("archive: skipping an unreadable sidecar")
    found.sort(key=lambda s: (s.taken_at, s.media_id))
    return found


def find_sidecar(root: Path, media_id: str) -> Sidecar | None:
    return next((s for s in scan_archive(root) if s.media_id == media_id), None)


class PostedLog:
    """media id -> Discord message id (None = deliberately skipped), kept as an append-only JSONL.

    Restarts reload it, so nothing is posted twice. A crash between sending a message and
    recording it can repost that one story; the label is keyed by media id so that's harmless.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.first_run = not self.path.exists()
        self._posted: dict[str, int | None] = {}
        if not self.first_run:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    doc = json.loads(line)
                    mid = doc["media_id"]
                    self._posted[str(mid)] = doc.get("message_id")
                except (ValueError, KeyError, TypeError):
                    continue

    def __contains__(self, media_id: str) -> bool:
        return media_id in self._posted

    def message_id(self, media_id: str) -> int | None:
        return self._posted.get(media_id)

    def record(self, media_id: str, message_id: int | None, reason: str = "") -> None:
        doc: dict[str, Any] = {"media_id": media_id, "message_id": message_id}
        if reason:
            doc["skipped"] = reason
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(doc) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._posted[media_id] = message_id
        self.first_run = False

    def mark_started(self) -> None:
        """Create the file even when nothing was posted, so the next start isn't a 'first run'."""
        if not self.path.exists():
            self.path.touch(mode=0o600)
        self.first_run = False


def select_new(
    sidecars: Iterable[Sidecar],
    posted: PostedLog,
    since: datetime | None = None,
    backlog_max: int = 10,
) -> tuple[list[Sidecar], list[Sidecar]]:
    """(to post oldest first, to skip for good). Only the first start skips a backlog.

    `since` drops older stories entirely (nothing recorded; they stay out on every scan). On the
    first start (no posted-log yet), only the newest `backlog_max` are posted and the rest are
    returned to be recorded as skipped, so a large archive doesn't flood the channel.
    """
    fresh = [s for s in sidecars if s.media_id not in posted and (since is None or s.taken_at >= since)]
    fresh.sort(key=lambda s: (s.taken_at, s.media_id))
    if not posted.first_run or len(fresh) <= backlog_max:
        return fresh, []
    cut = len(fresh) - backlog_max
    return fresh[cut:], fresh[:cut]


@dataclass(frozen=True)
class MediaChoice:
    path: Path | None
    kind: str  # "image", "video" or "none"
    note: str = ""  # shown in the message when the media can't be attached


def choose_media(sc: Sidecar, max_bytes: int) -> MediaChoice:
    """The file to upload. New archives hold images only; .mp4 handling is kept for old ones: the video when it fits, otherwise the image; says why if neither."""
    files = [sc.folder / name for name in sc.files if (sc.folder / name).is_file()]
    videos = [f for f in files if f.suffix.lower() in _VIDEO_EXTS]
    images = [f for f in files if f.suffix.lower() in _IMAGE_EXTS]
    note = ""
    for video in videos:
        if video.stat().st_size <= max_bytes:
            return MediaChoice(video, "video")
        note = f"Video too large to upload ({video.stat().st_size // 1_000_000} MB); see archive file {video.name}"
    for image in images:
        if image.stat().st_size <= max_bytes:
            return MediaChoice(image, "image", note)
        note = note or f"Image too large to upload; see archive file {image.name}"
    if not note:
        note = "No media was archived" + (f" ({sc.download_error})" if sc.download_error else "")
    return MediaChoice(None, "none", note)
