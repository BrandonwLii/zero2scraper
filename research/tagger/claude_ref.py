"""Pipeline 4, the accuracy reference: Claude Sonnet through headless Claude Code on the user's Pro plan.

One ``claude -p`` call per story, run on the workstation, with the same system prompt, context text
and JSON schema as the local models. The story still is copied into an empty temporary directory and
Claude reads it with the Read tool (the only tool allowed). Replies are cached under
``$TAGGER_WORK/raw/claude-<run>/``; a cached story is never sent again.

It stops at the first usage-limit or authentication error and never retries in a loop.
The user approved sending these story images to Anthropic for this comparison (2026-10-08).

    .venv/bin/python claude_ref.py sonnet-v1 [--limit 3]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time

from story_watch.evaluation.data import load_stories

from common import ARCHIVE, RAW, story_inputs
from prompting import DEFAULT_PROMPT, SCHEMA, system_prompt, user_text

LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "out of extra usage", "credit balance", "/upgrade", "login", "authenticat")


def ask(inp, prompt: str, model: str, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="tag-") as tmp:
        shutil.copyfile(inp.image, f"{tmp}/story.jpg")
        text = user_text(inp).replace("The story image is attached.", "The story image is the file ./story.jpg: read it first.")
        cmd = ["claude", "-p", text, "--model", model, "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
               "--system-prompt", system_prompt(prompt), "--tools", "Read", "--allowedTools", "Read",
               "--no-session-persistence", "--max-turns", "4", "--strict-mcp-config", "--setting-sources", ""]
        start = time.perf_counter()
        proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout)
        wall = time.perf_counter() - start
    try:
        rec = json.loads(proc.stdout)
    except ValueError:
        rec = {"is_error": True, "result": (proc.stdout or proc.stderr)[-500:]}
    rec["wall_s"] = wall
    rec["returncode"] = proc.returncode
    return rec


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run", help="cache label suffix, e.g. sonnet-v1")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--limit", type=int, default=0, help="stop after this many new calls (0 = all)")
    p.add_argument("--timeout", type=float, default=300)
    args = p.parse_args()
    out = RAW / f"claude-{args.run}"
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    calls = 0
    for s in load_stories(ARCHIVE).stories:
        path = out / f"{s.media_id}.json"
        if path.exists():
            continue
        if args.limit and calls >= args.limit:
            break
        rec = ask(story_inputs(s), args.prompt, args.model, args.timeout)
        calls += 1
        result = str(rec.get("result", ""))
        if rec.get("is_error") or rec["returncode"] != 0:
            if any(w in result.lower() for w in LIMIT_WORDS):
                print(f"stopping: usage-limit or auth error after {calls} calls ({rec.get('subtype', 'error')})", file=sys.stderr)
                return 3
            rec = {"error": rec.get("subtype") or "error", "duration_ms": rec.get("duration_ms"), "wall_s": rec["wall_s"]}
        keep = {k: rec.get(k) for k in ("structured_output", "result", "duration_ms", "duration_api_ms", "num_turns",
                                         "total_cost_usd", "usage", "wall_s", "error", "subtype")}
        path.write_text(json.dumps(keep), encoding="utf-8")
        print(f"{calls}: {rec.get('wall_s', 0):.1f}s turns={rec.get('num_turns')} {'error' if keep.get('error') else 'ok'}", flush=True)
    print(f"{calls} calls made")
    return 0


if __name__ == "__main__":
    sys.exit(main())
