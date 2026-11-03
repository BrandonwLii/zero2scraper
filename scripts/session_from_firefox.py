#!/usr/bin/env python3
"""Turn a Firefox cookies.sqlite into an instaloader session file (stdlib only).

  python3 -I scripts/session_from_firefox.py COOKIES_SQLITE OUT_FILE [--quiet-same]

instaloader's session file is a pickled dict of cookie name -> value. Needs the
`sessionid` and `ds_user_id` cookies. Prints cookie NAMES only, never values.
The previous OUT_FILE is kept as OUT_FILE.prev.
"""

from __future__ import annotations

import os
import pickle
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HOSTS = (".instagram.com", "instagram.com", "www.instagram.com")
REQUIRED = ("sessionid", "ds_user_id")


def _expired(expiry: int, now: float) -> bool:
    """Firefox stores expiry in seconds (older) or milliseconds (newer). Skip a cookie only
    if it is in the past under both readings; 0 means a session cookie and is kept."""
    if not expiry:
        return False
    return expiry < now and expiry / 1000 < now


def cookies_from_db(path) -> dict[str, str]:
    src = Path(path)
    tmp = tempfile.mkdtemp(prefix="sw-cookies-")
    try:
        os.chmod(tmp, 0o700)
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(src) + suffix)
            if f.exists():
                shutil.copy2(f, Path(tmp) / ("cookies.sqlite" + suffix))
        db = Path(tmp) / "cookies.sqlite"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(HOSTS))
            rows = con.execute(
                f"SELECT name, value, expiry FROM moz_cookies WHERE host IN ({marks}) ORDER BY rowid",
                HOSTS,
            ).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    now = time.time()
    out: dict[str, str] = {}
    for name, value, expiry in rows:
        if _expired(int(expiry or 0), now):
            continue
        out[name] = value
    return out


def _write_atomic(out: Path, data: dict[str, str]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(data, f)
        if out.exists():
            prev = out.with_name(out.name + ".prev")
            shutil.copy2(out, prev)
            os.chmod(prev, 0o600)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: session_from_firefox.py COOKIES_SQLITE OUT_FILE", file=sys.stderr)
        return 2
    src, out = Path(argv[0]), Path(argv[1])
    try:
        cookies = cookies_from_db(src)
    except (OSError, sqlite3.Error) as e:
        print(f"cannot read the Firefox cookie database ({type(e).__name__})", file=sys.stderr)
        return 2
    if not all(cookies.get(k) for k in REQUIRED):
        print("no Instagram login in this Firefox profile; log in on instagram.com first", file=sys.stderr)
        return 2
    if out.exists():
        try:
            with open(out, "rb") as f:
                old = pickle.load(f)
            same = isinstance(old, dict) and old.get("sessionid") == cookies["sessionid"]
            print(f"sessionid: {'same' if same else 'changed'}")
        except Exception:
            print("sessionid: changed")
    _write_atomic(out, cookies)
    print(f"wrote session with {len(cookies)} cookies")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
