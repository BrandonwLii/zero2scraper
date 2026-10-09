"""Fetch each job link once and record what each extractor got.

    research/job_pages/.venv/bin/python research/job_pages/run.py LINKS.json [--refresh]

LINKS.json is a list of URLs, built locally from story-cache or the archive and kept
outside the repo. Results go to research/job_pages/cache/ (gitignored): they hold
posting text and URLs, so they must never be committed. Only aggregates are printed.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from extract import Posting, extract  # noqa: E402
from fetch import Blocked, Fetcher  # noqa: E402

CACHE = Path(__file__).parent / "cache"


def ats_of(url: str) -> str:
    host = (requests.utils.urlparse(url).hostname or "").lower()
    for name in ("greenhouse", "lever", "ashbyhq", "myworkdayjobs", "smartrecruiters", "icims", "eightfold",
                 "oraclecloud", "workable", "jobvite"):
        if name in host:
            return name.replace("ashbyhq", "ashby").replace("myworkdayjobs", "workday")
    return "company site"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("links")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    CACHE.mkdir(exist_ok=True)
    links = json.loads(Path(args.links).read_text())
    f = Fetcher()
    results = []
    for url in links:
        key = CACHE / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".json")
        if key.exists() and not args.refresh:
            results.append(json.loads(key.read_text()))
            continue
        t0 = time.monotonic()
        try:
            p = extract(url, f)
        except Blocked as e:
            p = Posting(source="none", status="blocked", notes=[str(e)])
        except (requests.RequestException, OSError, UnicodeError) as e:
            p = Posting(source="none", status="error", notes=[type(e).__name__])
        rec = {"url": url, "ats": ats_of(url), "seconds": round(time.monotonic() - t0, 2),
               **dataclasses.asdict(p), "has_description": p.has_description}
        key.write_text(json.dumps(rec, indent=1))
        results.append(rec)
        time.sleep(1)  # be polite
    print(f"{len(results)} links, {f.requests} HTTP requests this run")
    by = collections.defaultdict(collections.Counter)
    for r in results:
        by[r["ats"]][r["status"] + ("" if not r["has_description"] or r["status"] != "ok" else "")] += 1
        by[r["ats"]]["_n"] += 1
    for ats, c in sorted(by.items(), key=lambda kv: -kv[1]["_n"]):
        n = c.pop("_n")
        print(f"{ats:14} n={n:2}  " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    src = collections.Counter((r["source"], r["status"]) for r in results)
    print("by extractor:", dict(src))
    ok = sum(r["has_description"] for r in results)
    print(f"full description: {ok}/{len(results)}")


if __name__ == "__main__":
    main()
