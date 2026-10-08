#!/usr/bin/env bash
# Deploy from your workstation: stream this repo into the LXC via the Proxmox
# host (over Tailscale) and run install.sh there. The LXC itself needs no
# Tailscale or SSH.
#
#   PVE_HOST=root@proxmox CTID=120 ./deploy/push.sh
set -euo pipefail

PVE_HOST="${PVE_HOST:?set PVE_HOST, e.g. root@proxmox (your Tailscale name)}"
CTID="${CTID:-120}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Ship the local .env as the server's config, minus the TEST_* settings that
# only scripts/resend_story.py reads. install.sh installs and then deletes it.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
extra_files=(.deploy-log)
if [[ -f .env ]]; then
    (umask 077; grep -v '^TEST_' .env > "$tmp/.env.deploy" || true)
    extra_files+=(.env.deploy)
    # Refuse to ship a .env the service would reject (it would fail to start).
    .venv/bin/python -m story_watch.main --check-config "$tmp/.env.deploy" \
        || { echo "not deploying: fix .env first" >&2; exit 1; }
fi

# install.sh posts this to Discord once the service is back up, with the commits
# since the last deploy (it remembers that one) picked out of this recent history.
commit="$(git rev-parse --short HEAD)"
sha="$(git rev-parse HEAD)"
[[ -z "$(git status --porcelain)" ]] || commit+="-dirty"
git log --first-parent -n 100 --format='%H%x09%s' HEAD > "$tmp/.deploy-log"

tar czf - --exclude .git --exclude .venv --exclude .env --exclude __pycache__ \
    --exclude '*.egg-info' --exclude .pytest_cache --exclude story-cache \
    --exclude 'story-dump*' --exclude '*.har' --exclude 'session-*' --exclude data --exclude '*.db' \
    . -C "$tmp" "${extra_files[@]}" \
  | ssh "$PVE_HOST" "pct exec $CTID -- bash -c '
        rm -rf /root/story-watch-src && mkdir -p /root/story-watch-src &&
        tar xzf - -C /root/story-watch-src &&
        DEPLOY_COMMIT=$commit DEPLOY_SHA=$sha bash /root/story-watch-src/deploy/install.sh'"
