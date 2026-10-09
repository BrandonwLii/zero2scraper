#!/usr/bin/env python3
"""Send prepared chat requests to a llama-server, one at a time, and save each reply with its latency.

Standard library only, so it runs unchanged inside CT 120 (Python 3.11). Input: a JSON-lines file of
``{"media_id", "body"}`` (make_requests.py). Output: ``<out>/<media id>.json`` with the reply text,
the wall-clock latency and llama-server's own timings. Already-answered stories are skipped, so an
interrupted run resumes. A request that fails or times out is recorded with its error type only.

    python3 run_requests.py requests.jsonl out/ --server http://127.0.0.1:8090 --timeout 120
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def post(server: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(server.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("requests", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--server", default=os.environ.get("LLAMA_SERVER", "http://127.0.0.1:8090"))
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--only", help="comma-separated media ids")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    done = failed = 0
    for line in args.requests.read_text(encoding="utf-8").splitlines():
        req = json.loads(line)
        mid = req["media_id"]
        path = args.out / f"{mid}.json"
        if (only and mid not in only) or path.exists():
            continue
        start = time.perf_counter()
        try:
            resp = post(args.server, req["body"], args.timeout)
            rec = {"media_id": mid, "latency_s": time.perf_counter() - start,
                   "content": resp["choices"][0]["message"]["content"], "timings": resp.get("timings"),
                   "usage": resp.get("usage")}
            done += 1
        except Exception as e:  # noqa: BLE001 - record the type only
            rec = {"media_id": mid, "latency_s": time.perf_counter() - start, "error": type(e).__name__}
            failed += 1
        path.write_text(json.dumps(rec), encoding="utf-8")
        print(f"{done + failed}: {rec['latency_s']:.1f}s{' ' + rec['error'] if 'error' in rec else ''}", flush=True)
    print(f"done {done}, failed {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
