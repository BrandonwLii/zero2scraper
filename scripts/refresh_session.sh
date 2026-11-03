#!/usr/bin/env bash
# Refresh the burner's Instagram session after logging in with Firefox on Windows.
#
#   scripts/refresh_session.sh [--dry-run] [--force]
#
# Env: PVE_HOST (default root@pve), CTID (default 120), FIREFOX_COOKIES (path to cookies.sqlite;
# auto-detected when there is exactly one default-release profile).
# --dry-run: only steps 1-4 (probe ssh, check Firefox, read IG_USER, write the local session).
# --force:   skip the "Firefox is running" check.
# Exit codes: 2 setup/cookie problem, 3 ssh needs approval, 4 Firefox running,
#             5 watcher reports a session/Instagram error, 6 no verdict within 180 s.
# Never prints cookie values, the webhook or the session contents.
set -euo pipefail

PVE_HOST="${PVE_HOST:-root@pve}"
CTID="${CTID:-120}"
DRY=0
FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    *) echo "usage: $0 [--dry-run] [--force]" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cookies="${FIREFOX_COOKIES:-}"
if [ -z "$cookies" ]; then
  shopt -s nullglob
  found=(/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*.default-release/cookies.sqlite)
  shopt -u nullglob
  if [ "${#found[@]}" -ne 1 ]; then
    echo "found ${#found[@]} Firefox cookie databases; set FIREFOX_COOKIES=/path/to/cookies.sqlite" >&2
    exit 2
  fi
  cookies="${found[0]}"
fi
[ -f "$cookies" ] || { echo "no such file: FIREFOX_COOKIES" >&2; exit 2; }

echo "==> checking ssh to $PVE_HOST"
if ! probe="$(timeout 15 ssh -o BatchMode=yes "$PVE_HOST" true 2>&1)"; then
  printf '%s\n' "$probe"
  echo "ssh to $PVE_HOST failed; approve any URL shown above, then re-run" >&2
  exit 3
fi

if [ "$FORCE" -eq 0 ] && command -v tasklist.exe >/dev/null 2>&1; then
  if tasklist.exe 2>/dev/null | grep -qi 'firefox\.exe'; then
    echo "close Firefox so its cookies are flushed (or use --force)" >&2
    exit 4
  fi
fi

echo "==> reading IG_USER from CT $CTID"
ig_user="$(timeout 30 ssh -o BatchMode=yes "$PVE_HOST" \
  "pct exec $CTID -- sh -c 'grep -m1 \"^IG_USER=\" /opt/story-watch/.env | cut -d= -f2-'" | tr -d '\r\n' || true)"
if [ -z "$ig_user" ]; then
  echo "IG_USER is empty or unreadable in the CT's .env" >&2
  exit 2
fi
if ! [[ "$ig_user" =~ ^[A-Za-z0-9._]+$ ]]; then
  echo "IG_USER has unexpected characters; refusing" >&2
  exit 2
fi

echo "==> extracting the Instagram session from Firefox"
sess_dir="$HOME/.config/instaloader"
mkdir -p "$sess_dir"
chmod 700 "$sess_dir"
sess="$sess_dir/session-$ig_user"
python3 -I "$HERE/session_from_firefox.py" "$cookies" "$sess" || exit $?

if [ "$DRY" -eq 1 ]; then
  echo "==> dry run: stopping before the push"
  exit 0
fi

echo "==> pushing the session to the CT"
tmp="$(timeout 30 ssh -o BatchMode=yes "$PVE_HOST" 'mktemp /root/.sw-session.XXXXXX' | tr -d '\r\n')"
if ! [[ "$tmp" =~ ^/root/\.sw-session\.[A-Za-z0-9]+$ ]]; then
  echo "unexpected temp path from host" >&2
  exit 2
fi
cleanup() { timeout 30 ssh -o BatchMode=yes "$PVE_HOST" "rm -f $tmp" >/dev/null 2>&1 || true; }
trap cleanup EXIT
timeout 60 scp -q -o BatchMode=yes "$sess" "$PVE_HOST:$tmp"
dest="/opt/story-watch/.config/instaloader/session-$ig_user"
timeout 60 ssh -o BatchMode=yes "$PVE_HOST" "trap 'rm -f $tmp' EXIT; set -e
if pct exec $CTID -- test -f $dest; then
  pct exec $CTID -- cp -p $dest $dest.prev
fi
pct push $CTID $tmp $dest --user storywatch --group storywatch --perms 0600"

echo "==> restarting story-watch"
# the CT's own clock and timezone, since journalctl --since runs there
since="$(timeout 60 ssh -o BatchMode=yes "$PVE_HOST" \
  "pct exec $CTID -- sh -c 'date \"+%Y-%m-%d %H:%M:%S\"; systemctl restart story-watch'" | head -n1 | tr -d '\r\n')"
if ! [[ "$since" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9:]{8}$ ]]; then
  echo "restart failed or no timestamp from the CT" >&2
  exit 2
fi

echo "==> waiting for the first cycle (up to 180 s)"
deadline=$((SECONDS + 180))
while [ "$SECONDS" -lt "$deadline" ]; do
  sleep 5
  logs="$(timeout 30 ssh -o BatchMode=yes "$PVE_HOST" \
    "pct exec $CTID -- journalctl -u story-watch --since '$since' --no-pager -o cat" 2>/dev/null \
    | grep -vi webhook || true)"
  # last verdict line wins: a later error overrides an earlier success and vice versa
  verdict="$(printf '%s\n' "$logs" | grep -E 'story page has [0-9]+ item|Instagram session problem|Instagram error|unexpected error' | tail -n1 || true)"
  if [ -n "$verdict" ]; then
    if [[ "$verdict" =~ story\ page\ has\ ([0-9]+)\ item ]]; then
      echo "==> back online (${BASH_REMATCH[1]} items on the story page)"
      exit 0
    fi
    echo "==> failed: ${verdict:0:200}" >&2
    exit 5
  fi
done
echo "==> no verdict within 180 s; check: ssh $PVE_HOST pct exec $CTID -- journalctl -u story-watch -n 100 --no-pager" >&2
exit 6
