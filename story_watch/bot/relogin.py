"""Re-login requests from Discord buttons (pure; no discord imports).

The bot only writes `request` (`start` or `done`) and reads `status` in RELOGIN_DIR, a directory
shared with the Proxmox host. The host's path unit does the rest. The bot never sees cookies or
the session. Status lines look like `<state> <epoch> <message>`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

REQUEST = "request"
STATUS = "status"
REPORTED = "status-reported"  # epoch of the last `failed` status already DMed (bot-owned)
ACTIONS = ("start", "done")
DEBOUNCE_SECONDS = 120
STATES = ("browser_starting", "browser_ready", "imported", "failed")
MESSAGE_MAX = 200

NOT_OWNER = "Only the owner can use the re-login buttons."
BUSY = "A request was sent less than 2 minutes ago. Give it a moment."
FAILED_WRITE = "Could not send the request (see the bot log)."
DISABLED = "Re-login buttons are not set up (RELOGIN_DIR is empty)."


def start_reply(url: str) -> str:
    return (
        f"Starting the login browser. In ~30 s open {url} , "
        "log in to the burner, clear any prompt, then press **I've logged in**."
    )


DONE_REPLY = "Importing the session; I'll DM you when the watcher is back online (or still failing)."


def is_owner(user_id: int, alert_user_id: int | None) -> bool:
    return alert_user_id is not None and user_id == alert_user_id


def write_request(directory: Path, action: str, now: float | None = None) -> str:
    """Atomically write the request file. Returns "ok", "busy" (a request under 2 min old is
    still there) or "error"."""
    if action not in ACTIONS:
        return "error"
    now = time.time() if now is None else now
    path = directory / REQUEST
    try:
        age = now - path.stat().st_mtime
        if age < DEBOUNCE_SECONDS:
            return "busy"
    except FileNotFoundError:
        pass
    except OSError:
        return "error"
    tmp = directory / f".{REQUEST}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="ascii") as f:
            f.write(action + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return "error"
    return "ok"


@dataclass(frozen=True)
class ReloginStatus:
    state: str
    epoch: int
    message: str


def parse_status(text: str) -> ReloginStatus | None:
    parts = text.strip().split(None, 2)
    if len(parts) < 2 or parts[0] not in STATES or not parts[1].isdigit():
        return None
    message = "".join(c for c in (parts[2] if len(parts) > 2 else "") if c.isprintable())
    return ReloginStatus(parts[0], int(parts[1]), message[:MESSAGE_MAX])


def read_status(directory: Path) -> ReloginStatus | None:
    try:
        return parse_status((directory / STATUS).read_text(encoding="utf-8", errors="replace")[:1000])
    except OSError:
        return None


def _reported(directory: Path) -> int:
    try:
        return int((directory / REPORTED).read_text().strip())
    except (OSError, ValueError):
        return 0


def failed_to_report(directory: Path) -> ReloginStatus | None:
    """A `failed` status newer than the last one reported, else None."""
    status = read_status(directory)
    if status is None or status.state != "failed" or status.epoch <= _reported(directory):
        return None
    return status


def failed_text(status: ReloginStatus) -> str:
    return f"Re-login failed: {status.message or 'unknown error'}"


def mark_reported(directory: Path, epoch: int) -> None:
    tmp = directory / f".{REPORTED}.tmp"
    tmp.write_text(f"{epoch}\n")
    os.replace(tmp, directory / REPORTED)
