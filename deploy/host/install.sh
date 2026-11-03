#!/usr/bin/env bash
# Runs on the Proxmox host as root (push.sh runs it before the CT install). Idempotent.
# Installs the re-login plumbing: the shared dir mounted into CT 120, the path unit and script
# that drive the login browser (CT 121), and provisions CT 121 when it exists and setup.sh changed.
#   bash /root/story-watch-src/deploy/host/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_CTID="${CTID:-120}"
LOGIN_CTID="${LOGIN_CTID:-121}"
SHARE=/var/lib/story-watch/relogin
LIB=/usr/local/lib/story-watch
BOT_UID=100996  # 100000 + storywatch-bot's uid (996) in the unprivileged CT 120
BOT_GID=100994

echo "==> host: shared re-login dir"
install -d -m 0755 /var/lib/story-watch
install -d -o "$BOT_UID" -g "$BOT_GID" -m 0770 "$SHARE"
want="$SHARE,mp=/opt/story-watch/relogin"
if pct config "$APP_CTID" | grep -q "^mp0: $want\$"; then
    echo "    CT $APP_CTID already mounts it"
elif pct config "$APP_CTID" | grep -q '^mp0:'; then
    echo "    WARNING: CT $APP_CTID has a different mp0; not touching it. Mount $SHARE at /opt/story-watch/relogin yourself." >&2
else
    pct set "$APP_CTID" -mp0 "$want"
    echo "    NOTE: CT $APP_CTID needs a restart to see the mount: pct reboot $APP_CTID"
fi

echo "==> host: relogin script and units"
install -d -m 0755 "$LIB"
install -m 0755 "$SRC/deploy/host/relogin.sh" "$LIB/relogin.sh"
install -m 0755 "$SRC/scripts/session_from_firefox.py" "$LIB/session_from_firefox.py"
install -m 0644 "$SRC/deploy/host/story-watch-relogin.path" /etc/systemd/system/story-watch-relogin.path
install -m 0644 "$SRC/deploy/host/story-watch-relogin.service" /etc/systemd/system/story-watch-relogin.service
systemctl daemon-reload
systemctl enable story-watch-relogin.path >/dev/null
systemctl restart story-watch-relogin.path

echo "==> host: login CT $LOGIN_CTID"
if ! pct status "$LOGIN_CTID" >/dev/null 2>&1; then
    echo "    CT $LOGIN_CTID does not exist; skipping (see HUMANS.md to create it)"
    exit 0
fi
sum="$(sha256sum "$SRC/deploy/login-ct/setup.sh" | cut -d' ' -f1)"
marker=/var/lib/story-watch/login-ct-setup.sha256
if [[ "$(cat "$marker" 2>/dev/null || true)" == "$sum" ]]; then
    echo "    setup.sh unchanged since the last run"
    exit 0
fi
was_running=1
if [[ "$(pct status "$LOGIN_CTID" | awk '{print $2}')" != running ]]; then
    was_running=0
    pct start "$LOGIN_CTID"
fi
for _ in $(seq 1 30); do pct exec "$LOGIN_CTID" -- true >/dev/null 2>&1 && break; sleep 2; done
pct push "$LOGIN_CTID" "$SRC/deploy/login-ct/setup.sh" /root/setup.sh --perms 0700
if pct exec "$LOGIN_CTID" -- bash /root/setup.sh; then
    echo "$sum" > "$marker"
else
    echo "    WARNING: login CT setup failed; will retry on the next push" >&2
fi
if [[ "$was_running" -eq 0 ]]; then pct shutdown "$LOGIN_CTID" --timeout 30 || true; fi
