"""Main loop: fetch -> diff -> notify -> mark, with jitter, backoff and alerts."""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
from datetime import date, datetime
from typing import Mapping

from dotenv import dotenv_values

from .config import Config, ConfigError, load_config
from .archive import Archive
from .classify import Category, Classifier, build_classifier
from .instagram import InstagramClient, InstagramError, SessionError, StoryItem
from .jobs import JobTitles, add_job_info
from .notify import COLOR_INFO, COLOR_OK, DiscordError, Notifier
from .store import Store

log = logging.getLogger("story_watch")

BACKOFF_CAP = 3600


class Backoff:
    """Doubles the wait on each consecutive failure, capped; reset on success."""

    def __init__(self, cap: float = BACKOFF_CAP):
        self.cap = cap
        self.current: float | None = None

    def fail(self, base: float) -> float:
        self.current = min(self.cap, (self.current or base) * 2)
        return self.current

    def reset(self) -> None:
        self.current = None


class Watcher:
    def __init__(
        self,
        cfg: Config,
        ig: InstagramClient,
        store: Store,
        notifier: Notifier,
        rng=None,
        jobs: JobTitles | None = None,
        classifier: Classifier | None = None,
        notifiers: Mapping[str, Notifier] | None = None,
        archive: Archive | None = None,
    ):
        """`notifier` posts to server 1 and sends all alerts. `notifiers` maps the other
        destination names to senders (default: one per DISCORD_WEBHOOK_<n>)."""
        self.cfg = cfg
        self.notifiers = {cfg.destinations[0].name: notifier}
        for dest in cfg.destinations[1:]:
            self.notifiers[dest.name] = (notifiers or {}).get(dest.name) or Notifier(dest.webhook)
        self.jobs = jobs
        self.archive = archive
        self.classifier = classifier or build_classifier(cfg.classifier)
        self.ig = ig
        self.store = store
        self.notifier = notifier
        self.rng = rng or random.Random()
        self.backoff = Backoff()
        self.consecutive_failures = 0
        self.failure_alerted = False
        self.session_alerted = False
        self.last_heartbeat: date | None = None

    def base_wait(self) -> float:
        return self.rng.uniform(self.cfg.min_wait, self.cfg.max_wait)

    def run_cycle(self) -> int:
        """Check every target once. Returns number of items sent to every server.

        A server whose send fails is skipped for the rest of the cycle (keeping its
        posts in order); the others carry on. Raises DiscordError at the end if any
        failed, and the item stays unseen so only the servers that missed it retry.

        New items are archived (if enabled) once, after the loop over all targets, even when
        a later target raises, and before the final DiscordError, so archiving can't delay a post.
        """
        sent = 0
        failed: dict[str, DiscordError] = {}  # destination name -> first error
        classified: list[tuple[StoryItem, Category]] = []  # (item, category), archived at the end
        try:
            for target in self.cfg.targets:
                items = self.ig.fetch_story_items(target)
                if not self.store.is_seeded(target):
                    self.store.seed(target, items)
                    log.info("seeded @%s with %d current item(s), no notifications", target, len(items))
                    continue
                new = sorted((i for i in items if not self.store.is_seen(i.media_id)), key=lambda i: i.taken_at)
                if new:
                    # Links/mentions are nice to have: a failed page fetch still notifies.
                    try:
                        new = self.ig.with_extras(target, new)
                    except InstagramError as e:
                        log.warning("@%s: sending without links/mentions: %s", target, e)
                    if self.jobs is not None:
                        new = [add_job_info(i, self.jobs) for i in new]
                for item in new:
                    category = self._classify(item)
                    classified.append((item, category))
                    if category not in self.cfg.notify_categories:
                        self.store.mark_skipped(item)
                        log.info("@%s item %s: %s, filtered out by NOTIFY_*", target, item.media_id, category.value)
                        continue
                    done = self.store.delivered_to(item.media_id)
                    for dest in self.cfg.destinations:
                        if dest.key in done or dest.name in failed:
                            continue
                        try:
                            self.notifiers[dest.name].story(
                                item, category=category, roles=self.cfg.roles_for(category, dest)
                            )
                        except DiscordError as e:
                            log.warning("@%s item %s: server %s failed: %s", target, item.media_id, dest.name, e)
                            failed[dest.name] = e
                            continue
                        self.store.mark_delivered(item.media_id, dest.key)
                        done.add(dest.key)
                    if len(done) < len(self.cfg.destinations):
                        continue  # left unseen, retried (and reclassified) next cycle
                    self.store.mark_seen(item)
                    sent += 1
                    log.info("notified @%s item %s", target, item.media_id)
                if not new:
                    log.info("@%s: %d item(s), nothing new", target, len(items))
        finally:
            # After every target's posts, so a slow CDN can't delay any of them (the URLs are
            # still fresh this cycle), and still when a later target raises.
            for item, category in classified:
                self._archive(item, category)
        pruned = self.store.prune()
        if pruned:
            log.info("pruned %d old row(s)", pruned)
        if failed:
            raise DiscordError("; ".join(f"server {name}: {e}" for name, e in failed.items()))
        return sent

    def _archive(self, item: StoryItem, category: Category) -> None:
        if self.archive is None:
            return
        try:
            self.archive.save(item, category, self.cfg.classifier)
        except Exception as e:  # save() already catches; archiving must never touch delivery
            log.warning("archive failed for %s (%s)", item.media_id, type(e).__name__)

    def _classify(self, item: StoryItem) -> Category:
        try:
            category = Category(self.classifier.classify(item))
        except Exception as e:  # a broken/slow classifier must not stop notifications
            log.warning("classifier failed on %s (%s); using misc", item.media_id, type(e).__name__)
            return Category.MISC
        log.info("item %s classified as %s", item.media_id, category.value)
        return category

    def step(self) -> float:
        """Run one cycle and return how long to sleep before the next."""
        try:
            self.run_cycle()
        except SessionError as e:
            log.error("Instagram session problem: %s", e)
            self._on_failure(e, session=True)
            return self.backoff.fail(self.base_wait())
        except InstagramError as e:
            log.warning("Instagram error: %s", e)
            self._on_failure(e)
            return self.backoff.fail(self.base_wait())
        except DiscordError as e:
            log.warning("Discord delivery failed: %s", e)
            self._on_failure(e)
            return self.base_wait()
        except Exception as e:
            log.exception("unexpected error")
            self._on_failure(e)
            return self.base_wait()
        self._on_success()
        return self.base_wait()

    def _on_failure(self, err: Exception, session: bool = False) -> None:
        self.consecutive_failures += 1
        if session and not self.session_alerted:
            self.session_alerted = self._try_alert(
                "Instagram session needs re-login",
                f"Error: `{err}`\n\nInside the LXC run:\n"
                f"`sudo -u storywatch /opt/story-watch/.venv/bin/instaloader --login={self.cfg.ig_user}`\n"
                "The watcher keeps backing off and picks up the new session automatically.",
            )
        elif not session and self.consecutive_failures >= self.cfg.fail_alert_threshold and not self.failure_alerted:
            self.failure_alerted = self._try_alert(
                "Story watcher failing",
                f"{self.consecutive_failures} consecutive failures. Last error: `{type(err).__name__}: {err}`",
            )

    def _on_success(self) -> None:
        if self.failure_alerted or self.session_alerted:
            self._try_alert("Story watcher recovered", "Checks are succeeding again.", COLOR_OK)
        self.consecutive_failures = 0
        self.failure_alerted = False
        self.session_alerted = False
        self.backoff.reset()

    def _try_alert(self, title: str, description: str, color: int | None = None) -> bool:
        try:
            if color is None:
                self.notifier.alert(title, description)
            else:
                self.notifier.alert(title, description, color)
            return True
        except DiscordError as e:
            log.error("could not send alert %r: %s", title, e)
            return False

    def maybe_heartbeat(self, now: datetime | None = None) -> None:
        if self.cfg.heartbeat_hour is None:
            return
        now = now or datetime.now().astimezone()
        if now.hour == self.cfg.heartbeat_hour and self.last_heartbeat != now.date():
            status = "healthy" if self.consecutive_failures == 0 else f"{self.consecutive_failures} consecutive failures"
            if self._try_alert(
                "Story watcher heartbeat",
                f"Still alive. Watching: {', '.join('@' + t for t in self.cfg.targets)}. Status: {status}.",
                COLOR_INFO,
            ):
                self.last_heartbeat = now.date()


def run_forever(watcher: Watcher, stop: threading.Event) -> None:
    log.info("starting: %s", watcher.cfg)
    while not stop.is_set():
        wait = watcher.step()
        watcher.maybe_heartbeat()
        log.info("next check in %.0fs", wait)
        stop.wait(wait)
    log.info("shutting down")


CHANGELOG_MAX = 15


def deploy_message(commit: str, log_lines: list[str] | None = None, since: str = "") -> str:
    """Deploy announcement. `log_lines` are "<sha>\t<subject>", newest first (push.sh's
    `git log`); `since` is the previously deployed sha, so the changelog is what's new."""
    text = f"Commit `{commit}` is running."
    if log_lines is None:
        return text
    entries = [line.rstrip("\n").partition("\t") for line in log_lines if line.strip()]
    # Only ever list commits since the last deploy; if that's unknown, list none.
    prev = next((i for i, (sha, _, _) in enumerate(entries) if since and sha.startswith(since)), None)
    if not since:
        header, new = "No previous deploy recorded, so no changelog.", []
    elif prev is None:
        header, new = f"Previous deploy `{since[:7]}` isn't in the recent history, so no changelog.", []
    else:
        header, new = "Changes since the last deploy:", entries[:prev]
        if not new:
            header = "No new commits since the last deploy."
    lines = [f"- `{sha[:7]}` {subject[:100]}" for sha, _, subject in new[:CHANGELOG_MAX]]
    if len(new) > CHANGELOG_MAX:
        lines.append(f"- …and {len(new) - CHANGELOG_MAX} more")
    if commit.endswith("-dirty"):
        lines.append("- plus uncommitted changes")
    return "\n".join([text, "", header, *lines])


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # urllib3 debug logs include request paths (i.e. the webhook token)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="story-watch")
    parser.add_argument("--test-notify", action="store_true", help="send a test Discord message and exit")
    parser.add_argument("--once", action="store_true", help="run a single check cycle and exit")
    parser.add_argument("--deploy-notify", metavar="COMMIT", help="announce a deploy of COMMIT on Discord and exit")
    parser.add_argument("--changelog", metavar="FILE", type=argparse.FileType("r", encoding="utf-8"),
                        help="with --deploy-notify: '<sha>\\t<subject>' lines, newest first ('-' = stdin)")
    parser.add_argument("--since", metavar="SHA", default="",
                        help="with --changelog: the previously deployed commit")
    parser.add_argument("--check-config", metavar="FILE",
                        help="validate the .env FILE (only, ignoring the environment) and exit")
    args = parser.parse_args(argv)

    _setup_logging()
    try:
        if args.check_config:
            cfg = load_config({k: v or "" for k, v in dotenv_values(args.check_config).items()})
        else:
            cfg = load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2
    if args.check_config:
        log.info("config ok, %d Discord server(s): %s", len(cfg.destinations), cfg)
        return 0

    notifier = Notifier(cfg.discord_webhook)
    if args.deploy_notify:
        # Every server hears about deploys; --test-notify and alerts stay on server 1.
        changelog = args.changelog.readlines() if args.changelog else None
        text = deploy_message(args.deploy_notify, changelog, args.since)
        ok = True
        for dest in cfg.destinations:
            sender = notifier if dest.name == "1" else Notifier(dest.webhook)
            try:
                sender.alert("story-watch deployed", text, COLOR_OK)
            except DiscordError as e:
                log.error("server %s: deploy notification failed: %s", dest.name, e)
                ok = False
                continue
            log.info("server %s: deploy notification sent", dest.name)
        return 0 if ok else 1
    if args.test_notify:
        try:
            notifier.alert("story-watch test", f"Webhook works. Targets: {', '.join(cfg.targets)}", COLOR_INFO)
        except DiscordError as e:
            log.error("test notification failed: %s", e)
            return 1
        log.info("test notification sent")
        return 0

    store = Store(cfg.db_path)
    ig = InstagramClient(cfg.ig_user, cfg.ig_session_path, userids=cfg.target_ids)
    archive = Archive(cfg.archive_dir, cfg.archive_max_mb) if cfg.archive_dir else None
    watcher = Watcher(cfg, ig, store, notifier, jobs=JobTitles(), archive=archive)
    try:
        if args.once:
            watcher.step()
            return 0 if watcher.consecutive_failures == 0 else 1

        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        run_forever(watcher, stop)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(cli())
