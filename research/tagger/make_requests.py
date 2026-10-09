"""Write the llama-server request bodies for every labelled story to ``$TAGGER_WORK/requests/<label>.jsonl``.

    .venv/bin/python make_requests.py vlm-v1 --mode vlm              # image + link/listing text
    .venv/bin/python make_requests.py ocrtext-v1 --mode ocr-text     # OCR text + link/listing text, no image
    .venv/bin/python make_requests.py vlmocr-v1 --mode vlm-ocr       # image + OCR hint + link/listing text

The files hold story images and text: they stay in the private work dir (and, for benchmarks, under
/root/bench/issue-10/data in CT 120, deleted afterwards).
"""

from __future__ import annotations

import argparse
import json
import sys

from story_watch.evaluation.data import load_stories

from common import ARCHIVE, WORK, load_ocr, story_inputs
from prompting import DEFAULT_PROMPT, chat_body


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("label")
    p.add_argument("--mode", choices=("vlm", "ocr-text", "vlm-ocr"), default="vlm")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = p.parse_args()
    out = WORK / "requests" / f"{args.label}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = []
    for s in load_stories(ARCHIVE).stories:
        inp = story_inputs(s)
        ocr = load_ocr(s.media_id) if args.mode != "vlm" else None
        if args.mode != "vlm" and ocr is None:
            print(f"error: no OCR text yet for a story; run ocr.py first", file=sys.stderr)
            return 1
        body = chat_body(inp, args.prompt, with_image=args.mode != "ocr-text", ocr_text=ocr)
        lines.append(json.dumps({"media_id": s.media_id, "body": body}))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(lines)} requests -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
