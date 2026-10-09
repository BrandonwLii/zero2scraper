"""Tagger outputs cached per (tagger, version, media id), so scoring again is cheap.

Layout: ``<root>/<tagger>/<version>/<media id>.json`` holding the tags, the latency and the cost
measured on the run that filled it. The default root is outside the repo; the cache holds tags
derived from story content. Changing a tagger's ``version`` starts a fresh cache for it. Failures
are never cached, so they are retried.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..tags import Tags

DEFAULT_CACHE_DIR = Path.home() / "story-watch-data" / "eval-cache"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class CacheEntry:
    tags: Tags
    latency_s: float
    cost_usd: Optional[float] = None


def _part(label: str, text: str) -> str:
    if not _SAFE.match(text):
        raise ValueError(f"{label} {text!r} must be letters, digits, '.', '_' or '-' (no path separators)")
    return text


class TagCache:
    def __init__(self, root: Path = DEFAULT_CACHE_DIR):
        self.root = Path(root)

    def _path(self, tagger: str, version: str, media_id: str) -> Path:
        return self.root / _part("tagger name", tagger) / _part("version", version) / (_part("media id", media_id) + ".json")

    def get(self, tagger: str, version: str, media_id: str) -> Optional[CacheEntry]:
        """The cached entry, or None if missing or unreadable."""
        path = self._path(tagger, version, media_id)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            cost = doc.get("cost_usd")
            return CacheEntry(Tags.from_dict(doc["tags"]), float(doc["latency_s"]), None if cost is None else float(cost))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, tagger: str, version: str, media_id: str, entry: CacheEntry) -> None:
        path = self._path(tagger, version, media_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        doc = {"tags": entry.tags.to_dict(), "latency_s": entry.latency_s, "cost_usd": entry.cost_usd}
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
