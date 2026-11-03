#!/usr/bin/env bash
# Timer-driven: when Firefox holds a NEW Instagram login and the watcher reports a sign-out
# outage, push the session (scripts/refresh_session.sh). Cheap checks first; no network unless
# a new login is seen and Firefox is closed. Logs to stdout (journald). Never prints cookie values.
#
#   scripts/auto_refresh.sh        (run every 2 min by story-watch-auto-refresh.timer)
#
# Needs ~/.config/story-watch/ig_user, written by one manual scripts/refresh_session.sh run.
# A failed refresh still updates the local session file, so it is not retried until Firefox
# has a newer login (the bot DMs "still failing after a fresh login").
set -euo pipefail

CTID="${CTID:-120}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_sw_common.sh
. "$HERE/_sw_common.sh"
log() { echo "auto_refresh: $*"; }

lockdir="$HOME/.cache/story-watch"
mkdir -p "$lockdir"
exec 9>"$lockdir/auto_refresh.lock"
flock -n 9 || exit 0

if [ ! -s "$IG_USER_CACHE" ]; then
  log "run refresh_session.sh once by hand first"
  exit 0
fi
ig_user="$(head -n1 "$IG_USER_CACHE" | tr -d '\r\n')"
if ! [[ "$ig_user" =~ ^[A-Za-z0-9._]+$ ]]; then
  log "cached IG_USER has unexpected characters; rerun refresh_session.sh"
  exit 0
fi

sw_find_cookies 2>/dev/null || exit 0
sess="$HOME/.config/instaloader/session-$ig_user"
rc=0
python3 -I "$HERE/session_from_firefox.py" --changed "$cookies" "$sess" || rc=$?
[ "$rc" -eq 0 ] || exit 0  # same login, or no login / unreadable: nothing to do

if command -v tasklist.exe >/dev/null 2>&1 && tasklist.exe 2>/dev/null | grep -qi 'firefox\.exe'; then
  log "new login seen; waiting for Firefox to close"
  exit 0
fi

if [ -z "${PVE_HOST:-}" ]; then
  if sw_lan_ok; then
    PVE_HOST=pve-lan
  elif timeout 15 ssh -o BatchMode=yes root@pve true >/dev/null 2>&1; then
    PVE_HOST=root@pve
  else
    log "new login seen but no ssh route to the host (pve-lan, root@pve); will retry"
    exit 0
  fi
fi
log "new login seen; host $PVE_HOST"

status="$(timeout 30 ssh -o BatchMode=yes "$PVE_HOST" \
  "pct exec $CTID -- runuser -u storywatch -- /opt/story-watch/.venv/bin/python -m story_watch.main --outage-status /opt/story-watch/.env" \
  2>/dev/null | tr -d '\r' | tail -n1 || true)"
case "$status" in
  "open session "*|"open flagged "*) log "outage: $status; pushing the session" ;;
  *) log "no sign-out outage (status: ${status:-none/unreadable}); not pushing"; exit 0 ;;
esac

rc=0
PVE_HOST="$PVE_HOST" CTID="$CTID" "$HERE/refresh_session.sh" || rc=$?
log "refresh_session.sh exited $rc"
exit 0
