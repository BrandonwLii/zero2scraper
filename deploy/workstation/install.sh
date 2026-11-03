#!/usr/bin/env bash
# Install the auto-refresh timer as a systemd USER unit (WSL workstation only; idempotent).
#   deploy/workstation/install.sh
# The repo path is taken from where this script lives and substituted for @REPO@.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
[ -x "$REPO/scripts/auto_refresh.sh" ] || { echo "missing $REPO/scripts/auto_refresh.sh" >&2; exit 1; }
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
sed "s|@REPO@|$REPO|g" "$HERE/story-watch-auto-refresh.service" > "$dest/story-watch-auto-refresh.service"
cp "$HERE/story-watch-auto-refresh.timer" "$dest/story-watch-auto-refresh.timer"
systemctl --user daemon-reload
systemctl --user enable --now story-watch-auto-refresh.timer
echo "installed; log: journalctl --user -u story-watch-auto-refresh -n 50"
