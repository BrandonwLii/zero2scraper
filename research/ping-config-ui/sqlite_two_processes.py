"""Throwaway check for issue #14: can a bot process and the watcher share state.db?

Runs two processes against a temporary SQLite file for a few seconds:

- "watcher" mimics store.py: short write transactions (mark_seen + clear
  deliveries) and, per story, a read of every user's ping preferences;
- "bot" mimics a /pings handler: one upsert per command, timed, since a Discord
  interaction has to be answered within 3 seconds.

It compares the current setup (rollback journal, Python's default 5 s busy
timeout) with WAL. Offline and stdlib-only: no Discord, no network.

    .venv/bin/python research/ping-config-ui/sqlite_two_processes.py
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

DURATION = 3.0  # seconds per scenario
HOLD = 0.02  # how long the fake watcher keeps each write transaction open (exaggerated)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (media_id TEXT PRIMARY KEY, target TEXT NOT NULL,
    taken_at REAL NOT NULL, notified_at REAL);
CREATE TABLE IF NOT EXISTS deliveries (media_id TEXT NOT NULL, destination TEXT NOT NULL,
    delivered_at REAL NOT NULL, PRIMARY KEY (media_id, destination));
CREATE TABLE IF NOT EXISTS ping_prefs (user_id TEXT NOT NULL, list TEXT NOT NULL,
    tag TEXT NOT NULL, PRIMARY KEY (user_id, list, tag));
"""


def connect(path: str, wal: bool, timeout: float) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=timeout)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def watcher(path: str, wal: bool, timeout: float, out: mp.Queue) -> None:
    db = connect(path, wal, timeout)
    writes = reads = errors = 0
    read_ms: list[float] = []
    end = time.monotonic() + DURATION
    n = 0
    while time.monotonic() < end:
        n += 1
        try:
            with db:  # same shape as Store.mark_seen
                db.execute("INSERT OR IGNORE INTO seen VALUES (?, 't', ?, ?)", (f"m{n}", time.time(), time.time()))
                db.execute("DELETE FROM deliveries WHERE media_id = ?", (f"m{n}",))
                time.sleep(HOLD)
            writes += 1
            t = time.monotonic()
            db.execute("SELECT user_id, list, tag FROM ping_prefs").fetchall()
            read_ms.append((time.monotonic() - t) * 1000)
            reads += 1
        except sqlite3.OperationalError:
            errors += 1
        time.sleep(HOLD)
    out.put(("watcher", writes, reads, errors, read_ms))


def bot(path: str, wal: bool, timeout: float, out: mp.Queue) -> None:
    db = connect(path, wal, timeout)
    writes = errors = 0
    write_ms: list[float] = []
    end = time.monotonic() + DURATION
    n = 0
    while time.monotonic() < end:
        n += 1
        t = time.monotonic()
        try:
            with db:
                db.execute("INSERT OR REPLACE INTO ping_prefs VALUES (?, 'ping', ?)", (str(n % 50), f"tag{n % 16}"))
            writes += 1
            write_ms.append((time.monotonic() - t) * 1000)
        except sqlite3.OperationalError:
            errors += 1
        time.sleep(0.005)
    out.put(("bot", writes, 0, errors, write_ms))


def scenario(name: str, wal: bool, timeout: float) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "state.db")
        connect(path, wal, timeout).close()
        out: mp.Queue = mp.Queue()
        procs = [mp.Process(target=f, args=(path, wal, timeout, out)) for f in (watcher, bot)]
        for p in procs:
            p.start()
        results = {r[0]: r for r in (out.get() for _ in procs)}
        for p in procs:
            p.join()
    _, w_writes, w_reads, w_err, read_ms = results["watcher"]
    _, b_writes, _, b_err, write_ms = results["bot"]
    print(f"\n{name}")
    print(f"  watcher: {w_writes} write txns, {w_reads} pref reads, {w_err} lock errors, "
          f"pref read max {max(read_ms, default=0):.1f} ms")
    if write_ms:
        print(f"  bot:     {b_writes} upserts, {b_err} lock errors, "
              f"median {statistics.median(write_ms):.1f} ms, max {max(write_ms):.1f} ms")
    else:
        print(f"  bot:     0 upserts, {b_err} lock errors")


if __name__ == "__main__":
    print(f"SQLite {sqlite3.sqlite_version}, {DURATION:.0f} s per scenario, watcher holds each write {HOLD * 1000:.0f} ms")
    scenario("rollback journal, no busy timeout (what goes wrong without one)", wal=False, timeout=0.0)
    scenario("rollback journal, 5 s busy timeout (Store today)", wal=False, timeout=5.0)
    scenario("WAL, 2 s busy timeout (proposed)", wal=True, timeout=2.0)
