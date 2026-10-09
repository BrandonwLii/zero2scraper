"""Benchmark a vision model behind llama-server: load time, latency per image, memory.

Starts llama-server on 127.0.0.1, waits for /health, then sends each image
RUNS times through the OpenAI-compatible chat endpoint with a tagging-style
prompt. Prints one JSON line of aggregates per configuration. Model output is
not printed (use --show to see it on synthetic images while debugging).

    .venv/bin/python bench_server.py --server DIR/llama-server --model M.gguf \
        --mmproj MM.gguf --threads 4 --image-max-tokens 512 IMG [IMG ...]

Stdlib only, apart from the images. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

PROMPT = (
    "This is an Instagram story image. Read all text on it, then answer with JSON only: "
    '{"text": "<all text, one line>", "company": "...", "role": "ML|SWE|PM|Other", '
    '"level": "Internship|New grad|Other", "sponsorship": "Sponsor or Canadian|No sponsor|Unknown", '
    '"post_type": "Event|Job posting|Process info|Misc"}'
)

# Facts written into the synthetic images by make_images.py. A crude legibility
# check: does the answer contain them (case-insensitive)? Not an accuracy score.
EXPECT = {
    "overlay": ["example corp", "intern", "toronto", "2027"],
    "screenshot": ["example corp", "intern", "toronto", "not available", "november 30"],
}


def rss_mib(pid: int) -> tuple[float, float]:
    """Current and peak resident memory of the server process, in MiB."""
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        k, _, v = line.partition(":")
        fields[k] = v.strip()
    kib = lambda k: int(fields[k].split()[0]) / 1024  # noqa: E731
    return kib("VmRSS"), kib("VmHWM")


def post(port: int, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--mmproj", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--image-max-tokens", type=int, default=0, help="0 = model default")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--extra", default="", help="extra llama-server flags, space-separated")
    ap.add_argument("--evict", action="store_true", help="drop the model files from the page cache first (cold load)")
    ap.add_argument("--time-v", default="", help="run the server under /usr/bin/time -v, writing its report here")
    ap.add_argument("--label", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("images", nargs="+")
    a = ap.parse_args()

    cmd = [
        a.server, "-m", a.model, "--mmproj", a.mmproj, "-t", str(a.threads), "-tb", str(a.threads),
        "-c", str(a.ctx), "--host", "127.0.0.1", "--port", str(a.port), "--no-webui", "-np", "1",
    ]
    if a.image_max_tokens:
        cmd += ["--image-max-tokens", str(a.image_max_tokens)]
    cmd += a.extra.split()
    env = dict(os.environ, LD_LIBRARY_PATH=str(Path(a.server).parent))
    if a.evict:
        for f in (a.model, a.mmproj):
            fd = os.open(f, os.O_RDONLY)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)
    if a.time_v:
        cmd = ["/usr/bin/time", "-v", "-o", a.time_v, *cmd]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server_pid = proc.pid
    try:
        while True:
            if proc.poll() is not None:
                raise SystemExit(f"llama-server exited with {proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.2)
        load_s = time.monotonic() - t0
        if a.time_v:  # the server is the only child of time
            server_pid = int(Path(f"/proc/{proc.pid}/task/{proc.pid}/children").read_text().split()[0])
        idle_rss, _ = rss_mib(server_pid)

        lat, prompt_ms, gen_ms, prompt_n, gen_n, json_ok = [], [], [], [], [], 0
        facts_hit = facts_total = 0
        for img in a.images:
            uri = "data:image/jpeg;base64," + base64.b64encode(Path(img).read_bytes()).decode()
            for _ in range(a.runs):
                body = {
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": uri}},
                        {"type": "text", "text": PROMPT},
                    ]}],
                    "max_tokens": a.max_tokens, "temperature": 0, "cache_prompt": False,
                }
                t = time.monotonic()
                resp = post(a.port, body, timeout=900)
                lat.append(time.monotonic() - t)
                tm = resp.get("timings", {})
                prompt_ms.append(tm.get("prompt_ms", 0))
                gen_ms.append(tm.get("predicted_ms", 0))
                prompt_n.append(tm.get("prompt_n", 0))
                gen_n.append(tm.get("predicted_n", 0))
                text = resp["choices"][0]["message"]["content"] or ""
                try:
                    json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
                    json_ok += 1
                except ValueError:
                    pass
                want = EXPECT.get(Path(img).stem, [])
                facts_total += len(want)
                facts_hit += sum(w in text.lower() for w in want)
                if a.show:
                    print(text)
        cur, peak = rss_mib(server_pid)
    finally:
        os.kill(server_pid, signal.SIGTERM)
        proc.wait(timeout=30)
    time_v_rss = None
    if a.time_v:
        for line in Path(a.time_v).read_text().splitlines():
            if "Maximum resident set size" in line:
                time_v_rss = round(int(line.rsplit(":", 1)[1]) / 1024)

    med = lambda xs: round(statistics.median(xs), 2)  # noqa: E731
    print(json.dumps({
        "label": a.label or Path(a.model).name, "threads": a.threads, "ctx": a.ctx, "extra": a.extra or None, "image_max_tokens": a.image_max_tokens or None,
        "load_s": round(load_s, 2), "cold": a.evict, "idle_rss_mib": round(idle_rss), "peak_rss_mib": round(peak), "time_v_max_rss_mib": time_v_rss,
        "requests": len(lat), "first_lat_s": round(lat[0], 2), "lat_s_median": med(lat), "lat_s_max": round(max(lat), 2),
        "prompt_tokens_median": med(prompt_n), "prompt_s_median": med([x / 1000 for x in prompt_ms]),
        "gen_tokens_median": med(gen_n), "gen_tok_per_s": round(sum(gen_n) / (sum(gen_ms) / 1000), 1),
        "json_ok": f"{json_ok}/{len(lat)}", "facts_found": f"{facts_hit}/{facts_total}",
    }))


if __name__ == "__main__":
    main()
