#!/usr/bin/env bash
# Runs on the Proxmox host as root, triggered by story-watch-relogin.path when the bot writes
# /var/lib/story-watch/relogin/request ("start" or "done"), or with --stop from the 30 min timer.
# Drives the login browser in CT 121 and imports its Instagram session into CT 120.
# Logs to stdout (journald). Never prints cookie values or session contents.
set -uo pipefail

DIR=/var/lib/story-watch/relogin
APP_CTID="${CTID:-120}"
LOGIN_CTID="${LOGIN_CTID:-121}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COOKIES_PATH=/home/browser/profile/cookies.sqlite
SERVE_PORT=8080
BOT_UID=100996
BOT_GID=100994
TMP=""
trap '[ -z "$TMP" ] || rm -rf "$TMP"' EXIT

log() { echo "relogin: $*"; }

set_status() { # state message...
  local state="$1"; shift
  local t; t="$(mktemp "$DIR/.status.XXXXXX")" || return 0
  printf '%s %s %s\n' "$state" "$(date +%s)" "$*" > "$t"
  chown "$BOT_UID:$BOT_GID" "$t"; chmod 0660 "$t"
  mv -f "$t" "$DIR/status"
}

ct_running() { [ "$(pct status "$LOGIN_CTID" 2>/dev/null | awk '{print $2}')" = running ]; }

stop_browser() {
  tailscale serve --http=$SERVE_PORT off >/dev/null 2>&1 || true
  if ct_running; then
    pct exec "$LOGIN_CTID" -- systemctl stop ig-browser >/dev/null 2>&1 || true
  fi
}

stop_all() {
  stop_browser
  systemctl stop story-watch-relogin-timeout.timer >/dev/null 2>&1 || true
  if ct_running; then pct shutdown "$LOGIN_CTID" --timeout 30 >/dev/null 2>&1 || true; fi
}

fail() { log "failed: $*"; set_status failed "$*"; stop_all; exit 1; }

ct_ip() {
  pct exec "$LOGIN_CTID" -- hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n1
}

do_start() {
  pct status "$LOGIN_CTID" >/dev/null 2>&1 || fail "login CT $LOGIN_CTID does not exist"
  set_status browser_starting "starting the login browser"
  if ! ct_running; then pct start "$LOGIN_CTID" >/dev/null 2>&1 || fail "could not start the login CT"; fi
  local ip="" i
  for i in $(seq 1 30); do ip="$(ct_ip || true)"; [ -n "$ip" ] && break; sleep 2; done
  [ -n "$ip" ] || fail "the login CT got no IPv4 address"
  pct exec "$LOGIN_CTID" -- systemctl start ig-browser >/dev/null 2>&1 || fail "could not start the browser service"
  local up=0
  for i in $(seq 1 30); do
    if timeout 2 bash -c "exec 3<>/dev/tcp/$ip/6080" 2>/dev/null; then up=1; break; fi
    sleep 2
  done
  [ "$up" -eq 1 ] || fail "the login browser did not come up"
  tailscale serve --bg --http=$SERVE_PORT "http://$ip:6080" >/dev/null 2>&1 || fail "tailscale serve failed"
  systemctl stop story-watch-relogin-timeout.timer >/dev/null 2>&1 || true
  systemd-run --on-active=30min --unit=story-watch-relogin-timeout "$HERE/relogin.sh" --stop >/dev/null 2>&1 \
    || log "could not schedule the 30 min auto-stop"
  set_status browser_ready "login browser ready"
  log "browser ready"
}

do_done() {
  ct_running || fail "the login browser is not running"
  pct exec "$LOGIN_CTID" -- systemctl stop ig-browser >/dev/null 2>&1 || fail "could not stop the browser to flush cookies"
  TMP="$(mktemp -d)" || fail "no temp dir"
  chmod 0700 "$TMP"
  pct pull "$LOGIN_CTID" "$COOKIES_PATH" "$TMP/cookies.sqlite" >/dev/null 2>&1 || fail "no cookie database in the login browser"
  if pct exec "$LOGIN_CTID" -- test -f "$COOKIES_PATH-wal" >/dev/null 2>&1; then
    pct pull "$LOGIN_CTID" "$COOKIES_PATH-wal" "$TMP/cookies.sqlite-wal" >/dev/null 2>&1 || true
  fi
  local rc=0
  python3 -I "$HERE/session_from_firefox.py" "$TMP/cookies.sqlite" "$TMP/session" >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 2 ] || fail "no Instagram login in the login browser"
  [ "$rc" -eq 0 ] || fail "could not read the login browser's cookies"

  local ig_user
  ig_user="$(pct exec "$APP_CTID" -- sh -c 'grep -m1 "^IG_USER=" /opt/story-watch/.env | cut -d= -f2-' 2>/dev/null | tr -d '\r\n')"
  [[ "$ig_user" =~ ^[A-Za-z0-9._]+$ ]] || fail "IG_USER missing or invalid in the watcher's .env"

  local dest="/opt/story-watch/.config/instaloader/session-$ig_user"
  if pct exec "$APP_CTID" -- test -f "$dest" >/dev/null 2>&1; then
    pct exec "$APP_CTID" -- cp -p "$dest" "$dest.prev" >/dev/null 2>&1 || fail "could not back up the old session"
  fi
  pct push "$APP_CTID" "$TMP/session" "$dest" --user storywatch --group storywatch --perms 0600 >/dev/null 2>&1 \
    || fail "could not install the session"

  local ver major ua
  ver="$(pct exec "$LOGIN_CTID" -- firefox-esr --version 2>/dev/null | head -n1)"
  major="$(printf '%s' "$ver" | grep -oE '[0-9]+' | head -n1)"
  if [[ "$major" =~ ^[0-9]{2,3}$ ]]; then
    ua="Mozilla/5.0 (X11; Linux x86_64; rv:$major.0) Gecko/20100101 Firefox/$major.0"
    pct exec "$APP_CTID" -- python3 -c '
import os, shutil, sys
p = "/opt/story-watch/.env"
ua = sys.argv[1]
shutil.copy2(p, p + ".bak")
out, done = [], False
for line in open(p).read().splitlines(True):
    if line.startswith("IG_USER_AGENT="):
        out.append("IG_USER_AGENT=" + ua + "\n"); done = True
    else:
        out.append(line)
if not done:
    out.append("IG_USER_AGENT=" + ua + "\n")
st = os.stat(p)
tmp = p + ".tmp"
open(tmp, "w").write("".join(out))
os.chmod(tmp, st.st_mode & 0o7777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, p)
' "$ua" >/dev/null 2>&1 || log "could not update IG_USER_AGENT (continuing)"
  else
    log "could not read the Firefox version; IG_USER_AGENT unchanged"
  fi

  pct exec "$APP_CTID" -- systemctl restart story-watch >/dev/null 2>&1 || fail "could not restart the watcher"
  stop_all
  set_status imported "session imported, watcher restarted"
  log "imported"
}

do_stop() {
  local was_running=0
  if ct_running && pct exec "$LOGIN_CTID" -- systemctl is-active --quiet ig-browser >/dev/null 2>&1; then was_running=1; fi
  stop_all
  if [ "$was_running" -eq 1 ]; then set_status failed "login browser timed out after 30 min"; fi
  log "stopped (browser was running: $was_running)"
}

exec 8>/run/story-watch-relogin.lock
flock -w 400 8 || { log "lock timeout"; exit 1; }

if [ "${1:-}" = "--stop" ]; then do_stop; exit 0; fi

[ -f "$DIR/request" ] || exit 0  # the path unit also fires when we delete the file
req="$(head -c 16 "$DIR/request" | tr -d '\r\n ')"
rm -f "$DIR/request"
case "$req" in
  start) do_start ;;
  done) do_done ;;
  *) log "ignoring an invalid request" ;;
esac
