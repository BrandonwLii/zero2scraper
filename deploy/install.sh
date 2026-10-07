#!/usr/bin/env bash
# Install or update story-watch. Run as root inside the Debian 12 LXC, from a
# checkout of this repo. Safe to re-run: it's also the update command.
set -euo pipefail

APP_USER=storywatch
APP_DIR=/opt/story-watch
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> packages"
if ! dpkg -s python3-venv rsync >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q python3-venv rsync
fi

echo "==> user $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> directories"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$APP_DIR/data" "$APP_DIR/.config" "$APP_DIR/.config/instaloader"

echo "==> code -> $APP_DIR/app"
# Code is root-owned so the service user can't modify what it runs.
rsync -a --delete --exclude .git --exclude .venv --exclude .env --exclude '__pycache__' \
    --exclude '*.egg-info' --exclude .pytest_cache "$SRC_DIR/" "$APP_DIR/app/"
chown -R root:root "$APP_DIR/app"

echo "==> venv"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q --upgrade "$APP_DIR/app"

echo "==> .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
    install -o root -g "$APP_USER" -m 0640 "$SRC_DIR/.env.example" "$APP_DIR/.env"
    echo "    created $APP_DIR/.env from template; edit it before starting"
fi

echo "==> systemd unit"
install -o root -g root -m 0644 "$SRC_DIR/deploy/story-watch.service" /etc/systemd/system/story-watch.service
systemctl daemon-reload
systemctl enable story-watch >/dev/null

if systemctl is-active --quiet story-watch; then
    systemctl restart story-watch
    echo "==> restarted story-watch"
elif grep -q '^DISCORD_WEBHOOK=https://discord' "$APP_DIR/.env" \
     && ls "$APP_DIR/.config/instaloader"/session-* >/dev/null 2>&1; then
    systemctl start story-watch
    echo "==> started story-watch"
else
    cat <<EOF

Installed. Remaining one-time steps:
  1. sudo -u $APP_USER $APP_DIR/.venv/bin/instaloader --login=<burner>
  2. edit $APP_DIR/.env (IG_USER, DISCORD_WEBHOOK, TARGETS)
  3. cd $APP_DIR && sudo -u $APP_USER .venv/bin/story-watch --test-notify
  4. systemctl start story-watch && journalctl -u story-watch -f
EOF
fi
