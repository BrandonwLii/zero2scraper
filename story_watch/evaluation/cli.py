"""Command line for scripts/eval_tagger.py."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import metrics
from .cache import DEFAULT_CACHE_DIR, TagCache
from .data import LABELS_NAME, load_stories
from .report import build_report, render_markdown
from .runner import run_tagger
from .taggers import load_tagger

DEFAULT_ARCHIVE = Path.home() / "story-watch-data" / "archive"
DEFAULT_PING_CONFIGS = Path(__file__).resolve().parents[2] / "eval" / "ping_configs.toml"
DEFAULT_NO_POST = "post_type:misc"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score a tagger against the labeled archive.")
    p.add_argument("archive", nargs="?", type=Path, default=DEFAULT_ARCHIVE, help=f"archive directory (default {DEFAULT_ARCHIVE})")
    p.add_argument("--labels", type=Path, help=f"labels file (default <archive>/{LABELS_NAME})")
    p.add_argument("--tagger", default="rules", help="a classifier name, 'unsure', or package.module:factory (default rules)")
    p.add_argument("--ping-configs", type=Path, default=DEFAULT_PING_CONFIGS, help="TOML file of user configs")
    p.add_argument("--no-post", default=DEFAULT_NO_POST, help="comma-separated tag keys on the 'no post' list (stub until #13)")
    p.add_argument("--min-count", type=int, default=metrics.DEFAULT_MIN_COUNT, help="flag values with fewer labeled stories")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="tagger output cache (keep outside the repo)")
    p.add_argument("--no-cache", action="store_true", help="neither read nor write the cache")
    p.add_argument("--refresh", action="store_true", help="re-run the tagger and overwrite cached outputs")
    p.add_argument("--allow-stale-taxonomy", action="store_true", help="also score labels made under another taxonomy version")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p.add_argument("--out", type=Path, help="also write the output to this file")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        tagger = load_tagger(args.tagger)
        configs = metrics.load_ping_configs(args.ping_configs)
        no_post = metrics.parse_no_post(args.no_post.split(","))
    except (ValueError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    load = load_stories(args.archive, args.labels, args.allow_stale_taxonomy)
    if not load.stories:
        print(f"error: no labeled stories to score in {args.archive} ({dict(load.skipped)})", file=sys.stderr)
        return 1
    cache = None if args.no_cache else TagCache(args.cache_dir)
    preds = run_tagger(tagger, load.stories, cache, refresh=args.refresh)
    report = build_report(tagger.name, tagger.version, load, preds, configs, no_post, args.min_count)
    text = json.dumps(report, indent=2) if args.json else render_markdown(report)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0
