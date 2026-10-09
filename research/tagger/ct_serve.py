#!/usr/bin/env python3
"""Start llama-server, send one request file through it, record load time and peak memory, stop it.

Standard library only (CT 120 has no curl). Run inside the memory-limited scope from ct_bench.sh, so
``memory.peak`` of the scope's cgroup covers the server and this script.

    python3 ct_serve.py --server <llama-server> --model m.gguf --mmproj p.gguf --image-tokens 512 \
        --requests r.jsonl --out raw/<label> --label <label> --results results/
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8091


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    p = argparse.ArgumentParser()
    for a in ("--server", "--model", "--mmproj", "--requests", "--out", "--label", "--results"):
        p.add_argument(a, required=True)
    p.add_argument("--image-tokens", type=int, default=512)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--timeout", type=float, default=180)
    args = p.parse_args()
    results = Path(args.results)
    cg = Path("/sys/fs/cgroup" + Path("/proc/self/cgroup").read_text().strip().split(":", 2)[2])
    cmd = [args.server, "-m", args.model, "--host", "127.0.0.1", "--port", str(PORT), "-t", str(args.threads), "-tb", str(args.threads),
           "-c", "4096", "-np", "1", "--cache-ram", "0", "--load-mode", "none"]
    if args.mmproj != "none":
        cmd += ["--mmproj", args.mmproj, "--image-max-tokens", str(args.image_tokens)]
    t0 = time.perf_counter()
    with open(results / f"server.{args.label}.log", "w") as log:
        srv = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    try:
        while not healthy():
            if srv.poll() is not None:
                print("server exited during load", file=sys.stderr)
                return 1
            time.sleep(0.25)
        load_s = time.perf_counter() - t0
        with open(results / f"run.{args.label}.log", "w") as log:
            subprocess.run([sys.executable, str(Path(__file__).with_name("run_requests.py")), args.requests, args.out,
                            "--server", f"http://127.0.0.1:{PORT}", "--timeout", str(args.timeout)], stdout=log, stderr=subprocess.STDOUT)
        status = Path(f"/proc/{srv.pid}/status").read_text()
        hwm_kb = int(next(l.split()[1] for l in status.splitlines() if l.startswith("VmHWM")))
        try:
            peak = int((cg / "memory.peak").read_text())
        except OSError:
            peak = None
    finally:
        srv.terminate()
        srv.wait(timeout=30)
    rec = {"label": args.label, "load_s": round(load_s, 2), "server_vmhwm_mib": round(hwm_kb / 1024), "scope_peak_mib": None if peak is None else round(peak / 2**20)}
    with open(results / "runs.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
