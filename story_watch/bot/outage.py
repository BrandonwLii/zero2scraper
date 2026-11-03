"""Which outage DMs are due (pure; no discord imports). The watcher writes the outage row, the
bot sends what this returns and marks the field sent. Only exception class names appear in DMs."""

from __future__ import annotations

from dataclasses import dataclass

from ..store import Outage

LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"


@dataclass(frozen=True)
class OutageDM:
    field: str  # the Store.mark_dm_sent field to set after a successful send
    title: str
    body: str
    link_url: str | None = None


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} d {hours % 24} h"


def pending_dms(outage: Outage | None, now: float) -> list[OutageDM]:
    if outage is None:
        return []
    out: list[OutageDM] = []
    since = f"Started <t:{int(outage.started_at)}:R>, {outage.failures} failed checks."
    if outage.alert_due_at is not None and outage.dm_sent_at is None and outage.resolved_at is None:
        if outage.kind == "session":
            out.append(OutageDM(
                "dm_sent_at",
                "Instagram signed the burner out",
                "Log in on instagram.com in Firefox on the workstation, clear any prompt, then close Firefox. "
                "Then run `scripts/refresh_session.sh` in WSL (soon: picked up automatically).\n" + since,
                LOGIN_URL,
            ))
        elif outage.kind == "flagged":
            out.append(OutageDM(
                "dm_sent_at",
                "Instagram is throttling the burner",
                "The watcher is backing off on its own. Nothing to do unless this lasts more than ~6 hours; "
                "then log in with Firefox to clear any prompt and run `scripts/refresh_session.sh`.\n" + since,
                HOME_URL,
            ))
        else:
            out.append(OutageDM(
                "dm_sent_at",
                "Story watcher failing",
                f"{outage.failures} consecutive failures (`{outage.last_error_type}`). "
                "Probably needs a code fix: check `journalctl -u story-watch`.\n" + since,
            ))
    if outage.relogin_failed and outage.relogin_dm_sent_at is None and outage.resolved_at is None:
        out.append(OutageDM(
            "relogin_dm_sent_at",
            "Still failing after a fresh login",
            f"A new session was pushed but checks still fail (`{outage.last_error_type}`). "
            "This looks like an account flag or a code problem, not a sign-out.",
        ))
    if outage.resolved_at is not None and outage.dm_sent_at is not None and outage.recovered_dm_sent_at is None:
        n = outage.caught_up or 0
        out.append(OutageDM(
            "recovered_dm_sent_at",
            "Story watcher back online",
            f"Down for {_duration(outage.resolved_at - outage.started_at)}; "
            f"{n} stor{'y' if n == 1 else 'ies'} caught up.",
        ))
    return out
