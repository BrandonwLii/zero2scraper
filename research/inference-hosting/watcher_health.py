"""Summarise the watcher's journal over a time window as aggregates only.

Run inside the service container:
    python3 watcher_health.py --since "2026-10-08 23:00:00" [--until ...]

Prints one JSON line: cycles completed, WARNING/ERROR line counts, and how late
each cycle started compared with the "next check in Ns" it announced (a
measure of the watcher being starved). No log text is printed, because the
journal contains account names.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default="now")
    ap.add_argument("--unit", default="story-watch")
    a = ap.parse_args()
    out = subprocess.run(
        ["journalctl", "-u", a.unit, "--since", a.since, "--until", a.until, "-o", "short-iso", "--no-pager"],
        capture_output=True, text=True, check=True,
    ).stdout
    cycles = warnings = errors = 0
    lateness, due = [], None
    for line in out.splitlines():
        m = re.match(r"(\S+) ", line)
        if not m or line.startswith("--"):
            continue
        ts = datetime.fromisoformat(m.group(1)).timestamp()
        if " WARNING " in line:
            warnings += 1
        if " ERROR " in line or "Traceback" in line:
            errors += 1
        if "item(s)" in line:
            cycles += 1
            if due is not None:
                lateness.append(ts - due)
            due = None
        nxt = re.search(r"next check in (\d+)s", line)
        if nxt:
            due = ts + int(nxt.group(1))
    print(json.dumps({
        "unit": a.unit, "since": a.since, "cycles": cycles, "warnings": warnings, "errors": errors,
        "max_cycle_lateness_s": round(max(lateness), 1) if lateness else None,
    }))


if __name__ == "__main__":
    main()
