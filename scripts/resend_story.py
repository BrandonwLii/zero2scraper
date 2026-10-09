"""Re-send the Nth most recent story to a test webhook, to iterate on the embed.

    .venv/bin/python scripts/resend_story.py --list          # numbered list of cached stories
    .venv/bin/python scripts/resend_story.py -n 3            # send the 3rd most recent
    .venv/bin/python scripts/resend_story.py -n 1-5 --dry-run   # print payloads, send nothing
    .venv/bin/python scripts/resend_story.py --refresh ...   # re-fetch from Instagram first
    .venv/bin/python scripts/resend_story.py -n 2 --no-ping  # don't ping TEST_PING_USER_IDS

Instagram is hit only on the first run or with --refresh: the GraphQL items and
the raw story-page items are cached in story-cache/<target>.json, and every run
rebuilds the embed from that cache with the current code (link unwrapping, job
lookup, notify.py). Job pages are fetched live each run. Thumbnail URLs in the
cache expire after about a day; --refresh if images stop showing.

Reads IG_USER, IG_SESSION_PATH, TARGETS, CLASSIFIER and NOTIFY_* like the
service, plus TEST_DISCORD_WEBHOOK and TEST_PING_USER_IDS (comma-separated Discord
user ids) from the environment / .env. Every story is sent regardless of NOTIFY_* (it
says when the service would skip it). Every story pings exactly the TEST_PING_USER_IDS
users, in the test server. It never reads real users' ping preferences (it never opens
the service database), so a test can't ping them. Uses the same session file as the
service and never prints the webhook URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from story_watch.classify import build_classifier  # noqa: E402
from story_watch.config import WEBHOOK_PREFIXES, ConfigError, load_config  # noqa: E402
from story_watch.instagram import InstagramClient, InstagramError, StoryItem, apply_page_items  # noqa: E402
from story_watch.jobs import JobTitles, add_job_info  # noqa: E402
from story_watch.notify import DiscordError, Notifier, link_label, tags_text  # noqa: E402
from story_watch.tags import Tags  # noqa: E402


class _Capture:
    """Stands in for requests.Session in --dry-run: records the payload, returns 204."""

    status_code = 204
    headers: dict = {}

    def __init__(self):
        self.payloads = []

    def post(self, url, json=None, **kw):
        self.payloads.append(json)
        return self


def parse_ping_user_ids(raw: str) -> list[str]:
    """TEST_PING_USER_IDS: comma-separated Discord user ids (a pasted <@id> is accepted)."""
    ids = [p.strip().removeprefix("<@").removesuffix(">") for p in raw.split(",") if p.strip()]
    for uid in ids:
        if not (uid.isdigit() and 15 <= len(uid) <= 21):
            raise SystemExit("TEST_PING_USER_IDS must be comma-separated Discord user ids (Developer Mode, right-click a user)")
    return ids


def parse_target(env_targets: str, arg: str | None) -> tuple[str, int | None]:
    raw = arg or env_targets.split(",")[0]
    name, _, uid = raw.strip().lstrip("@").lower().partition(":")
    return name, int(uid) if uid.strip() else None


def fetch(cache: Path, ig_user: str, session_path: str | None, name: str, uid: int | None) -> None:
    client = InstagramClient(ig_user, session_path, userids={name: uid} if uid else None)
    items = client.fetch_story_items(name)
    page = client.story_page_items(name)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "items": [dict(asdict(i), taken_at=i.taken_at.isoformat()) for i in items],
                "page": page,
            },
            default=str,
        )
    )
    print(f"fetched {len(items)} item(s), {len(page)} with page data -> {cache}")


def load(cache: Path) -> tuple[list[StoryItem], dict]:
    data = json.loads(cache.read_text())
    items = []
    for raw in data["items"]:
        raw = dict(raw, taken_at=datetime.fromisoformat(raw["taken_at"]))
        # Drop derived fields so the current code recomputes them.
        for key in ("links", "mentions", "job_title", "company"):
            raw.pop(key, None)
        items.append(StoryItem(**raw))
    items = apply_page_items(items, data["page"])
    items.sort(key=lambda i: i.taken_at, reverse=True)
    return items, data


def parse_selection(spec: str, count: int) -> list[int]:
    picks: list[int] = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        picks.extend(range(int(lo), int(hi or lo) + 1))
    bad = [p for p in picks if not 1 <= p <= count]
    if bad:
        raise SystemExit(f"no story #{bad[0]}; there are {count} (1 = most recent)")
    return picks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", default="1", help="which stories, 1 = most recent; e.g. 3, 1-5, 2,4 (default 1)")
    ap.add_argument("--list", action="store_true", help="list cached stories and exit")
    ap.add_argument("--refresh", action="store_true", help="re-fetch from Instagram before sending")
    ap.add_argument("--dry-run", action="store_true", help="print the Discord payload instead of sending")
    ap.add_argument("--no-ping", action="store_true", help="don't ping the TEST_PING_USER_IDS users")
    ap.add_argument("--target", help="username[:userid] (default: first TARGETS entry)")
    ap.add_argument("--cache-dir", default="story-cache", help="where cached stories live (default story-cache/)")
    args = ap.parse_args()

    load_dotenv(find_dotenv(usecwd=True))
    name, uid = parse_target(os.environ.get("TARGETS", "zero2sudo"), args.target)
    cache = Path(args.cache_dir) / f"{name}.json"

    if args.refresh or not cache.exists():
        ig_user = os.environ.get("IG_USER", "").strip()
        if not ig_user:
            raise SystemExit("IG_USER is not set (env or .env)")
        try:
            fetch(cache, ig_user, os.environ.get("IG_SESSION_PATH", "").strip() or None, name, uid)
        except InstagramError as e:
            raise SystemExit(f"Instagram fetch failed: {e}")

    items, data = load(cache)
    if not items:
        raise SystemExit(f"no stories cached for @{name} (fetched {data['fetched_at']}); try --refresh")

    if args.list:
        print(f"@{name}: {len(items)} stories, cached {data['fetched_at']}")
        now = datetime.now(timezone.utc)
        for n, item in enumerate(items, 1):
            age = (now - item.taken_at).total_seconds() / 3600
            link = link_label(item.links[0]) if item.links else ""
            print(f"{n:3}  {item.media_id}  {age:5.1f}h ago  {'video' if item.is_video else 'photo'}  {link}")
        return 0

    webhook = os.environ.get("TEST_DISCORD_WEBHOOK", "").strip()
    if not args.dry_run and not webhook.startswith(WEBHOOK_PREFIXES):
        raise SystemExit("set TEST_DISCORD_WEBHOOK (env or .env) to a Discord webhook URL, or use --dry-run")

    try:
        # Only the classifier/filter settings matter here; satisfy the required keys.
        cfg = load_config({**os.environ, "IG_USER": "-", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/-"})
    except ConfigError as e:
        raise SystemExit(f"config error: {e}")
    ping_ids = [] if args.no_ping else parse_ping_user_ids(os.environ.get("TEST_PING_USER_IDS", ""))
    classifier = build_classifier(cfg.classifier)

    capture = _Capture() if args.dry_run else None
    notifier = Notifier(webhook or "https://discord.com/api/webhooks/dry-run", session=capture)
    jobs = JobTitles()
    for n in parse_selection(args.n, len(items)):
        item = add_job_info(items[n - 1], jobs)
        try:
            tags = classifier.classify(item)
        except Exception as e:
            print(f"  classifier failed ({type(e).__name__}); using unsure tags, as the service does")
            tags = Tags.unsure()
        skipped = "" if cfg.should_notify(tags) else "  (service would skip: NOTIFY_* off)"
        print(f"#{n} {item.media_id}: [{tags_text(tags)}]{skipped} company={item.company!r} "
              f"title={item.job_title!r} links={list(item.links)} pings={len(ping_ids)}")
        try:
            notifier.story(item, tags=tags, user_ids=ping_ids)
        except DiscordError as e:
            print(f"  send failed: {e}")
            return 1
        if capture:
            print(json.dumps(capture.payloads.pop(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
