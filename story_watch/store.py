"""SQLite state: which story items we've already handled."""

from __future__ import annotations

import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from .instagram import StoryItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    media_id    TEXT PRIMARY KEY,
    target      TEXT NOT NULL,
    taken_at    REAL NOT NULL,   -- unix seconds
    notified_at REAL             -- NULL when recorded silently on first run
);
-- Tracks which targets have been seeded, so a target with no stories on its
-- first check (or whose rows were all pruned) isn't re-seeded silently later.
CREATE TABLE IF NOT EXISTS targets (
    name      TEXT PRIMARY KEY,
    seeded_at REAL NOT NULL
);
-- Per-server deliveries of items not yet in `seen` (some webhook failed), so a
-- retry skips the servers that already have it. Cleared once the item is seen.
CREATE TABLE IF NOT EXISTS deliveries (
    media_id     TEXT NOT NULL,
    destination  TEXT NOT NULL,  -- Discord webhook id (not the token)
    delivered_at REAL NOT NULL,
    PRIMARY KEY (media_id, destination)
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def is_seeded(self, target: str) -> bool:
        return self._db.execute("SELECT 1 FROM targets WHERE name = ?", (target,)).fetchone() is not None

    def seed(self, target: str, items: Iterable[StoryItem]) -> None:
        with self._db:
            for item in items:
                self._insert(item, notified_at=None)
            self._db.execute(
                "INSERT OR IGNORE INTO targets (name, seeded_at) VALUES (?, ?)", (target, time.time())
            )

    def is_seen(self, media_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM seen WHERE media_id = ?", (media_id,)).fetchone() is not None

    def mark_seen(self, item: StoryItem, notified_at: float | None = None) -> None:
        with self._db:
            self._insert(item, notified_at if notified_at is not None else time.time())
            self._db.execute("DELETE FROM deliveries WHERE media_id = ?", (item.media_id,))

    def delivered_to(self, media_id: str) -> set[str]:
        rows = self._db.execute("SELECT destination FROM deliveries WHERE media_id = ?", (media_id,))
        return {r[0] for r in rows}

    def mark_delivered(self, media_id: str, destination: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO deliveries (media_id, destination, delivered_at) VALUES (?, ?, ?)",
                (media_id, destination, time.time()),
            )

    def mark_skipped(self, item: StoryItem) -> None:
        """Record an item deliberately not posted (filtered category); notified_at stays NULL."""
        with self._db:
            self._insert(item, notified_at=None)

    def _insert(self, item: StoryItem, notified_at: float | None) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO seen (media_id, target, taken_at, notified_at) VALUES (?, ?, ?, ?)",
            (item.media_id, item.target, item.taken_at.timestamp(), notified_at),
        )

    def prune(self, older_than: timedelta = timedelta(hours=48), now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - older_than.total_seconds()
        with self._db:
            cur = self._db.execute("DELETE FROM seen WHERE taken_at < ?", (cutoff,))
            # Leftovers of items that never reached every server (e.g. a deleted webhook)
            self._db.execute("DELETE FROM deliveries WHERE delivered_at < ?", (cutoff,))
        return cur.rowcount
