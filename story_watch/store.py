"""SQLite state: which story items we've already handled."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from .instagram import StoryItem
from .pings import PingPrefs
from .tags import Tags, parse_tag_key

log = logging.getLogger(__name__)

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
-- Tags from the first classification, so a delivery retry reuses them instead of
-- asking a nondeterministic classifier again (it could ping a different set of people).
CREATE TABLE IF NOT EXISTS story_tags (
    media_id TEXT PRIMARY KEY,
    taken_at REAL NOT NULL,
    tags     TEXT NOT NULL    -- Tags.to_dict() as JSON
);
-- Per-user ping preferences (#16), written by the bot and read by the watcher (#17).
-- `tag` is a qualified key such as "role:swe"; `list` is "ping" or "mute" (don't ping me).
-- A user row with no tag rows is a saved, empty list ("never pinged").
CREATE TABLE IF NOT EXISTS ping_users (
    user_id    TEXT PRIMARY KEY,  -- Discord user id
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ping_prefs (
    user_id TEXT NOT NULL REFERENCES ping_users(user_id) ON DELETE CASCADE,
    list    TEXT NOT NULL CHECK (list IN ('ping', 'mute')),
    tag     TEXT NOT NULL,
    PRIMARY KEY (user_id, list, tag)
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The bot writes this file too. WAL lets the watcher's reads proceed during a bot write;
        # the busy timeout covers the brief write-write overlap. Both statements are idempotent
        # (the journal mode persists in the file), so every start is also the migration.
        self._db = sqlite3.connect(str(path), timeout=10)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
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

    def mark_skipped(self, item: StoryItem) -> None:
        """Record an item deliberately not posted (filtered category); notified_at stays NULL."""
        with self._db:
            self._insert(item, notified_at=None)

    def get_tags(self, media_id: str) -> Tags | None:
        row = self._db.execute("SELECT tags FROM story_tags WHERE media_id = ?", (media_id,)).fetchone()
        if row is None:
            return None
        try:
            return Tags.from_dict(json.loads(row[0]))
        except (ValueError, TypeError) as e:  # unreadable row: classify again
            log.warning("stored tags for %s unreadable (%s); reclassifying", media_id, type(e).__name__)
            return None

    def save_tags(self, item: StoryItem, tags: Tags) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO story_tags (media_id, taken_at, tags) VALUES (?, ?, ?)",
                (item.media_id, item.taken_at.timestamp(), json.dumps(tags.to_dict())),
            )

    def _insert(self, item: StoryItem, notified_at: float | None) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO seen (media_id, target, taken_at, notified_at) VALUES (?, ?, ?, ?)",
            (item.media_id, item.target, item.taken_at.timestamp(), notified_at),
        )

    def prune(self, older_than: timedelta = timedelta(hours=48), now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - older_than.total_seconds()
        with self._db:
            cur = self._db.execute("DELETE FROM seen WHERE taken_at < ?", (cutoff,))
            self._db.execute("DELETE FROM story_tags WHERE taken_at < ?", (cutoff,))
        return cur.rowcount

    # -- ping preferences (#16) --------------------------------------------------------------

    def get_ping_prefs(self, user_id: str) -> PingPrefs | None:
        """The user's saved lists, or None if they never saved any. Keys that are no longer
        valid tag values (the taxonomy changed) are ignored."""
        if self._db.execute("SELECT 1 FROM ping_users WHERE user_id = ?", (user_id,)).fetchone() is None:
            return None
        return self._prefs_from_rows(
            self._db.execute("SELECT list, tag FROM ping_prefs WHERE user_id = ?", (user_id,)).fetchall()
        )

    def all_ping_prefs(self) -> dict[str, PingPrefs]:
        rows: dict[str, list[tuple[str, str]]] = {
            uid: [] for (uid,) in self._db.execute("SELECT user_id FROM ping_users")
        }
        for uid, lst, tag in self._db.execute("SELECT user_id, list, tag FROM ping_prefs"):
            rows.setdefault(uid, []).append((lst, tag))
        return {uid: self._prefs_from_rows(r) for uid, r in rows.items()}

    def set_ping_prefs(self, user_id: str, prefs: PingPrefs, now: float | None = None) -> None:
        """Replace the user's lists with `prefs` (as serialized by PingPrefs.to_keys)."""
        ping, mute = prefs.to_keys()
        with self._db:
            self._db.execute(
                "INSERT INTO ping_users (user_id, updated_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET updated_at = excluded.updated_at",
                (user_id, now if now is not None else time.time()),
            )
            self._db.execute("DELETE FROM ping_prefs WHERE user_id = ?", (user_id,))
            self._db.executemany(
                "INSERT INTO ping_prefs (user_id, list, tag) VALUES (?, ?, ?)",
                [(user_id, "ping", k) for k in ping] + [(user_id, "mute", k) for k in mute],
            )

    def clear_ping_prefs(self, user_id: str) -> bool:
        """Delete everything saved for the user. True if there was anything."""
        with self._db:
            cur = self._db.execute("DELETE FROM ping_users WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0

    @staticmethod
    def _prefs_from_rows(rows: Iterable[tuple[str, str]]) -> PingPrefs:
        ping: list[str] = []
        mute: list[str] = []
        for lst, tag in rows:
            try:
                parse_tag_key(tag)
            except ValueError:
                continue
            (ping if lst == "ping" else mute).append(tag)
        return PingPrefs.from_keys(ping, mute)
