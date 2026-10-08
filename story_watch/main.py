"""Main loop: fetch -> diff -> notify -> mark, with jitter, backoff and alerts."""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
from datetime import date, datetime

from .config import Config, ConfigError, load_config
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
    ):
        self.cfg = cfg
        self.jobs = jobs
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
        """Check every target once. Returns number of notifications sent."""
        sent = 0
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
                if category not in self.cfg.notify_categories:
                    self.store.mark_skipped(item)
                    log.info("@%s item %s: %s, filtered out by NOTIFY_*", target, item.media_id, category.value)
                    continue
                # raises DiscordError -> left unseen, retried (and reclassified) next cycle
                self.notifier.story(item, category=category, roles=self.cfg.roles_for(category))
                self.store.mark_seen(item)
                sent += 1
                log.info("notified @%s item %s", target, item.media_id)
            if not new:
                log.info("@%s: %d item(s), nothing new", target, len(items))
        pruned = self.store.prune()
        if pruned:
            log.info("pruned %d old row(s)", pruned)
        return sent

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
    args = parser.parse_args(argv)

    _setup_logging()
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2

    notifier = Notifier(cfg.discord_webhook)
    if args.test_notify or args.deploy_notify:
        if args.deploy_notify:
            title, text, color = "story-watch deployed", f"Commit `{args.deploy_notify}` is running.", COLOR_OK
        else:
            title, text, color = "story-watch test", f"Webhook works. Targets: {', '.join(cfg.targets)}", COLOR_INFO
        try:
            notifier.alert(title, text, color)
        except DiscordError as e:
            log.error("%s notification failed: %s", title, e)
            return 1
        log.info("%s notification sent", title)
        return 0

    store = Store(cfg.db_path)
    ig = InstagramClient(cfg.ig_user, cfg.ig_session_path, userids=cfg.target_ids)
    watcher = Watcher(cfg, ig, store, notifier, jobs=JobTitles())
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
