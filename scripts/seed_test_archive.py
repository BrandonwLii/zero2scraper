"""Drop fake stories into a scratch archive so the labeling bot has something to post.

    .venv/bin/python scripts/seed_test_archive.py                  # 3 stories in ~/story-watch-data/test-archive
    .venv/bin/python scripts/seed_test_archive.py --dir /tmp/arch  # somewhere else
    .venv/bin/python scripts/seed_test_archive.py --reset          # wipe the folder (incl. labels) first

Each run adds new stories (fresh media ids, taken "now"), so a bot that is already running
picks them up on its next scan. Sidecars use the watcher's real format (story_watch/archive.py)
with a generated placeholder PNG. No network. Never point this at the production archive.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DIR = Path.home() / "story-watch-data" / "test-archive"
TARGET = "test_target"

# (category the classifier "guessed", links, mentions, job_title, company, has image)
STORIES = [
    ("job_posting", ["https://example.com/careers/123"], [], "Software Engineer Intern", "Example Corp", True),
    ("misc", [], ["someone_else"], None, None, True),
    ("misc", [], [], None, None, False),  # no media: shows the "No media was archived" note
]


def placeholder_png(rgb: tuple[int, int, int], width: int = 270, height: int = 480) -> bytes:
    """A solid-colour PNG, built with the stdlib only."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def seed(root: Path) -> list[Path]:
    folder = root / TARGET
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base_id = int(now.timestamp()) * 1000
    written = []
    for i, (category, links, mentions, title, company, has_image) in enumerate(STORIES):
        taken_at = now - timedelta(minutes=len(STORIES) - i)
        media_id = str(base_id + i)
        stem = f"{taken_at.strftime('%Y%m%dT%H%M%SZ')}_{media_id}"
        files = []
        if has_image:
            (folder / f"{stem}.png").write_bytes(placeholder_png(((60 * i) % 256, 120, 200)))
            files.append(f"{stem}.png")
        doc = {
            "media_id": media_id,
            "target": TARGET,
            "taken_at": taken_at.isoformat(),
            "is_video": False,
            "links": links,
            "mentions": mentions,
            "job_title": title,
            "company": company,
            "classifier": "rules",
            "category": category,
            "files": files,
            "download_error": None if has_image else "fake story without media",
            "node": {"fake": True},
            "page_node": None,
        }
        sidecar = folder / f"{stem}.json"
        sidecar.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        written.append(sidecar)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help=f"archive folder (default {DEFAULT_DIR})")
    ap.add_argument("--reset", action="store_true", help="delete the folder first, including labels and posted log")
    args = ap.parse_args()
    root = args.dir.expanduser().resolve()
    if args.reset and root.exists():
        if root.name != "test-archive" and "test" not in root.name:
            print(f"refusing to --reset {root}: folder name doesn't look like a test archive", file=sys.stderr)
            return 1
        shutil.rmtree(root)
    for path in seed(root):
        print(path)
    print(f"\nARCHIVE_DIR={root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
