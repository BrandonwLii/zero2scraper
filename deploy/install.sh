#!/usr/bin/env bash
# Install or update story-watch. Run as root inside the Debian 12 LXC, from a
# checkout of this repo. Safe to re-run: it's also the update command.
set -euo pipefail

APP_USER=storywatch
# The Discord bot runs as its own user (#45), so it can't read .env (the Claude token, Instagram
# settings) or the Instagram session. It shares only data/ and the archive with the watcher, through
# SHARED_GROUP.
BOT_USER=storywatch-bot
SHARED_GROUP=storywatch-shared
APP_DIR=/opt/story-watch
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> packages"
if ! dpkg -s python3-venv rsync >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q python3-venv rsync
fi

echo "==> users $APP_USER and $BOT_USER, group $SHARED_GROUP"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"
fi
getent group "$SHARED_GROUP" >/dev/null || groupadd --system "$SHARED_GROUP"
if ! id "$BOT_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$BOT_USER"
fi
for u in "$APP_USER" "$BOT_USER"; do
    [[ " $(id -nG "$u") " == *" $SHARED_GROUP "* ]] || usermod -a -G "$SHARED_GROUP" "$u"
done

echo "==> directories"
# The bot may pass through the home directory (to the venv, data/ and the archive) but not into
# .config (the Instagram session); .env is root:$APP_USER 0640 and the bot isn't in that group.
install -d -o "$APP_USER" -g "$SHARED_GROUP" -m 0750 "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$APP_DIR/.config" "$APP_DIR/.config/instaloader"
# Shared with the bot. setgid: new files get the shared group, and both units' UMask=0007 makes them
# group-writable. The archive is also sticky, so neither user can remove or rename the other's files
# at its top level. (Not data/: SQLite deletes a -wal file the other service may have created.)
install -d -o "$APP_USER" -g "$SHARED_GROUP" -m 2770 "$APP_DIR/data"
install -d -o "$APP_USER" -g "$SHARED_GROUP" -m 3770 "$APP_DIR/archive"
# Claude's private HOME lives in data/ but stays the watcher's alone.
[[ ! -d "$APP_DIR/data/claude-home" ]] || install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$APP_DIR/data/claude-home"

echo "==> code -> $APP_DIR/app"
# Code is root-owned so the service user can't modify what it runs.
rsync -a --delete --exclude .git --exclude .venv --exclude .env --exclude .env.deploy --exclude .deploy-log --exclude '__pycache__' \
    --exclude '*.egg-info' --exclude .pytest_cache "$SRC_DIR/" "$APP_DIR/app/"
chown -R root:root "$APP_DIR/app"

echo "==> venv"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
# The ocr extra (RapidOCR, onnxruntime) is for CLASSIFIER=hybrid and claude (the fallback reads the
# image with it); it is small, so it is always installed.
"$APP_DIR/.venv/bin/pip" install -q --upgrade "$APP_DIR/app[ocr]"

echo "==> .env"
if [[ -f "$SRC_DIR/.env.deploy" ]]; then
    # Sent by push.sh from the workstation's .env; it replaces the server's.
    [[ -f "$APP_DIR/.env" ]] && cp -p "$APP_DIR/.env" "$APP_DIR/.env.bak"
    install -o root -g "$APP_USER" -m 0640 "$SRC_DIR/.env.deploy" "$APP_DIR/.env"
    rm -f "$SRC_DIR/.env.deploy"
    echo "    installed .env from workstation (previous one in .env.bak)"
elif [[ ! -f "$APP_DIR/.env" ]]; then
    install -o root -g "$APP_USER" -m 0640 "$SRC_DIR/.env.example" "$APP_DIR/.env"
    echo "    created $APP_DIR/.env from template; edit it before starting"
fi

# -- the hybrid tagger's model (CLASSIFIER=hybrid only) ------------------------------------------
# Pinned llama.cpp CPU build and Qwen3-VL-4B files, each checked by SHA-256. Sources, recorded
# 2026-10-10: the GitHub release asset digest for llama.cpp b11514 (the build #8 and #10 measured),
# and the Hugging Face LFS hashes of Qwen/Qwen3-VL-4B-Instruct-GGUF at the revision below (the files
# #10 scored). Changing any pin means re-scoring with scripts/eval_tagger.py first.
LLAMA_TAG=b11514
LLAMA_TGZ="llama-$LLAMA_TAG-bin-ubuntu-x64.tar.gz"
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/$LLAMA_TGZ"
LLAMA_SHA256=b2b617fb28e9d1444235c30bb09a3704789a1840e16ead58a5f7be6eeae8beef
HF_REPO=Qwen/Qwen3-VL-4B-Instruct-GGUF
HF_REV=1cd86afb9a95c410a6038ab3b40d8b578c892266
MODEL_FILE=Qwen3VL-4B-Instruct-Q4_K_M.gguf
MODEL_SHA256=66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a
MMPROJ_FILE=mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf
MMPROJ_SHA256=30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d
LLM_USER=storywatch-llm
MODELS_DIR="$APP_DIR/models"
LLAMA_DIR="$APP_DIR/llama.cpp"

# fetch_verified <url> <dest> <sha256>: download once; a stamp file with the checked hash makes
# re-runs skip both the download and re-hashing 2.5 GB.
fetch_verified() {
    local url=$1 dest=$2 sha=$3
    if [[ -f "$dest" && "$(cat "$dest.sha256" 2>/dev/null)" == "$sha" ]]; then
        return 0
    fi
    echo "    downloading $(basename "$dest")"
    curl -fL --retry 3 --proto '=https' --tlsv1.2 -o "$dest.part" "$url"
    if ! echo "$sha  $dest.part" | sha256sum -c --quiet -; then
        rm -f "$dest.part"
        echo "SHA-256 mismatch for $(basename "$dest"); not installed" >&2
        return 1
    fi
    mv -f "$dest.part" "$dest"
    echo "$sha" > "$dest.sha256"
}

# The same values the service accepts: any case, optional quotes, trailing spaces or CR.
if grep -qiE '^CLASSIFIER=["'"'"']?hybrid["'"'"']?[[:space:]]*$' "$APP_DIR/.env"; then
    echo "==> tagger model (CLASSIFIER=hybrid)"
    if ! dpkg -s libgomp1 curl ca-certificates >/dev/null 2>&1; then
        apt-get update -q
        apt-get install -y -q libgomp1 curl ca-certificates
    fi
    if ! id "$LLM_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$LLM_USER"
    fi
    install -d -o root -g root -m 0755 "$MODELS_DIR" "$LLAMA_DIR"
    fetch_verified "$LLAMA_URL" "$LLAMA_DIR/$LLAMA_TGZ" "$LLAMA_SHA256"
    if [[ ! -x "$LLAMA_DIR/llama-$LLAMA_TAG/llama-server" ]]; then
        tar xzf "$LLAMA_DIR/$LLAMA_TGZ" -C "$LLAMA_DIR"
        chown -R root:root "$LLAMA_DIR/llama-$LLAMA_TAG"
    fi
    ln -sfn "llama-$LLAMA_TAG" "$LLAMA_DIR/current"
    fetch_verified "https://huggingface.co/$HF_REPO/resolve/$HF_REV/$MODEL_FILE" "$MODELS_DIR/$MODEL_FILE" "$MODEL_SHA256"
    fetch_verified "https://huggingface.co/$HF_REPO/resolve/$HF_REV/$MMPROJ_FILE" "$MODELS_DIR/$MMPROJ_FILE" "$MMPROJ_SHA256"
    chmod 0644 "$MODELS_DIR/$MODEL_FILE" "$MODELS_DIR/$MMPROJ_FILE"

    install -o root -g root -m 0644 "$SRC_DIR/deploy/story-watch-llm.service" /etc/systemd/system/story-watch-llm.service
    systemctl daemon-reload
    systemctl enable story-watch-llm >/dev/null
    # Restart only when the unit, the binary or a model changed (or it isn't running): a reload
    # takes ~6 s, during which /health answers 503 and new stories get the fallback tags.
    llm_stamp="$(sha256sum "$SRC_DIR/deploy/story-watch-llm.service" | cut -d' ' -f1) $LLAMA_SHA256 $MODEL_SHA256 $MMPROJ_SHA256"
    if ! systemctl is-active --quiet story-watch-llm || [[ "$(cat "$LLAMA_DIR/.deployed" 2>/dev/null)" != "$llm_stamp" ]]; then
        systemctl reset-failed story-watch-llm 2>/dev/null || true
        systemctl restart story-watch-llm
        echo "$llm_stamp" > "$LLAMA_DIR/.deployed"
        echo "==> (re)started story-watch-llm"
    else
        echo "    story-watch-llm unchanged and running"
    fi
elif systemctl is-enabled --quiet story-watch-llm 2>/dev/null || systemctl is-active --quiet story-watch-llm 2>/dev/null; then
    echo "==> CLASSIFIER is not hybrid: stopping story-watch-llm (model files kept in $MODELS_DIR)"
    systemctl disable --now story-watch-llm >/dev/null
fi

# -- the Claude tagger's CLI (CLASSIFIER=claude only, #41) -----------------------------------------
# Claude Code's native Linux binary, pinned to the version the bake-off ran (2026-10-10) and checked
# by SHA-256. It is root-owned, never auto-updates (the classifier sets DISABLE_AUTOUPDATER) and runs
# as $APP_USER with HOME=$APP_DIR/data/claude-home, headless and with every tool disabled.
# To bump: pick a version, take linux-x64's "checksum" from
#   https://downloads.claude.ai/claude-code-releases/<version>/manifest.json
# set both below, try `scripts/resend_story.py --dry-run` with that version locally, then deploy.
# The previous version's directory is kept, so rolling back is changing the two lines back.
CLAUDE_CLI_VERSION=2.1.296
CLAUDE_CLI_SHA256=24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de
CLAUDE_CLI_URL="https://downloads.claude.ai/claude-code-releases/$CLAUDE_CLI_VERSION/linux-x64/claude"
CLAUDE_DIR="$APP_DIR/claude"

if grep -qiE '^CLASSIFIER=["'"'"']?claude["'"'"']?[[:space:]]*$' "$APP_DIR/.env"; then
    echo "==> Claude Code $CLAUDE_CLI_VERSION (CLASSIFIER=claude)"
    [[ "$(uname -m)" == x86_64 ]] || { echo "the pinned Claude Code build is linux-x64 only" >&2; exit 1; }
    if ! dpkg -s curl ca-certificates >/dev/null 2>&1; then
        apt-get update -q
        apt-get install -y -q curl ca-certificates
    fi
    install -d -o root -g root -m 0755 "$CLAUDE_DIR" "$CLAUDE_DIR/$CLAUDE_CLI_VERSION"
    fetch_verified "$CLAUDE_CLI_URL" "$CLAUDE_DIR/$CLAUDE_CLI_VERSION/claude" "$CLAUDE_CLI_SHA256"
    chown root:root "$CLAUDE_DIR/$CLAUDE_CLI_VERSION/claude"
    chmod 0755 "$CLAUDE_DIR/$CLAUDE_CLI_VERSION/claude"
    ln -sfn "$CLAUDE_CLI_VERSION" "$CLAUDE_DIR/current"
    # Claude's private HOME: only the service user can read it (the classifier also enforces 0700).
    install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$APP_DIR/data/claude-home"
    # A smoke test that makes no request: the binary runs here as the service user.
    if ! runuser -u "$APP_USER" -- env -i HOME="$APP_DIR/data/claude-home" PATH=/usr/bin:/bin DISABLE_AUTOUPDATER=1 \
            "$CLAUDE_DIR/current/claude" --version >/dev/null; then
        echo "Claude Code doesn't run in this CT; stories will get the fallback tags" >&2
    fi
    grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' "$APP_DIR/.env" \
        || echo "    CLAUDE_CODE_OAUTH_TOKEN is not set in $APP_DIR/.env; the service won't start (see HUMANS.md)" >&2
fi

# Run after restarting the watcher (its new umask) and before restarting the bot. Each step only
# touches what differs, so a second run changes nothing.
share_files() {
    # state.db and its WAL files: group read-write, whichever service created them (SQLite gives a
    # new -wal or -shm file the database's mode). A copy root made in a manual run goes to $APP_USER.
    local f
    for f in "$APP_DIR"/data/state.db "$APP_DIR"/data/state.db-wal "$APP_DIR"/data/state.db-shm; do
        [[ -e "$f" ]] || continue
        [[ "$(stat -c %U "$f")" != root ]] || chown "$APP_USER" "$f"
        [[ "$(stat -c %G:%a "$f")" == "$SHARED_GROUP:660" ]] || { chgrp "$SHARED_GROUP" "$f"; chmod 0660 "$f"; }
    done
    # The archive: the watcher's per-target folders and files become group-readable (not writable:
    # the folders are 2750). The bot's own files at the top level become the bot's.
    # The watcher is already running, so a file can vanish mid-scan (a .part renamed, prune): warn
    # instead of aborting under set -e, before the bot is migrated. The next deploy finishes it.
    find "$APP_DIR/archive" ! -group "$SHARED_GROUP" -exec chgrp -h "$SHARED_GROUP" {} + \
        || echo "    warning: some archive files kept their group; re-run install.sh" >&2
    find "$APP_DIR/archive" -mindepth 1 -maxdepth 1 -type d ! -perm 2750 -exec chmod 2750 {} + \
        || echo "    warning: some archive folders kept their mode; re-run install.sh" >&2
    find "$APP_DIR/archive" -mindepth 2 -type f ! -perm -g=r -exec chmod g+r {} + \
        || echo "    warning: some archive files aren't group-readable; re-run install.sh" >&2
    for f in "$APP_DIR/archive/labels.jsonl" "$APP_DIR/archive/label-posts.jsonl"; do
        [[ ! -f "$f" ]] || [[ "$(stat -c %U "$f")" == "$BOT_USER" ]] || chown "$BOT_USER" "$f"
    done
}

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

echo "==> shared files (data/, archive)"
share_files

echo "==> bot.env"
# Only the keys the bot reads (BOT_ENV_KEYS in story_watch/bot/config.py, an allowlist): never the
# Claude token or the Instagram settings. Root's file, readable by the bot's group only.
bot_env_tmp="$(mktemp "$APP_DIR/.bot.env.XXXXXX")"
if ! "$APP_DIR/.venv/bin/python" -I -m story_watch.bot.envfile "$APP_DIR/.env" > "$bot_env_tmp"; then
    rm -f "$bot_env_tmp"
    echo "could not derive $APP_DIR/bot.env from .env" >&2
    exit 1
fi
if cmp -s "$bot_env_tmp" "$APP_DIR/bot.env" && [[ "$(stat -c %U:%G:%a "$APP_DIR/bot.env")" == "root:$BOT_USER:640" ]]; then
    rm -f "$bot_env_tmp"
else
    install -o root -g "$BOT_USER" -m 0640 "$bot_env_tmp" "$APP_DIR/bot.env"
    rm -f "$bot_env_tmp"
    echo "    wrote $APP_DIR/bot.env"
fi

echo "==> bot unit"
install -o root -g root -m 0644 "$SRC_DIR/deploy/story-watch-bot.service" /etc/systemd/system/story-watch-bot.service
systemctl daemon-reload
if grep -q '^DISCORD_BOT_TOKEN=.' "$APP_DIR/bot.env"; then
    systemctl enable story-watch-bot >/dev/null
    if systemctl is-active --quiet story-watch-bot; then
        systemctl restart story-watch-bot
        echo "==> restarted story-watch-bot"
    else
        systemctl reset-failed story-watch-bot 2>/dev/null || true
        systemctl start story-watch-bot
        echo "==> started story-watch-bot"
    fi
    sleep 3
    systemctl is-active --quiet story-watch-bot \
        || echo "story-watch-bot is not running; see journalctl -u story-watch-bot" >&2
else
    echo "    DISCORD_BOT_TOKEN is not set in $APP_DIR/.env; skipping story-watch-bot (see HUMANS.md)"
fi

# push.sh passes the deployed commit and recent git log; announce it with the
# commits since the last deploy once the service has stayed up.
if [[ -n "${DEPLOY_COMMIT:-}" ]] && systemctl is-active --quiet story-watch; then
    sleep 5
    if ! systemctl is-active --quiet story-watch; then
        echo "story-watch stopped after the restart; see journalctl -u story-watch" >&2
        exit 1
    fi
    changelog=() log_file=/dev/null
    if [[ -f "$SRC_DIR/.deploy-log" ]]; then
        log_file="$SRC_DIR/.deploy-log"
        changelog=(--changelog - --since "$(cat "$APP_DIR/deployed-commit" 2>/dev/null || true)")
    fi
    (cd "$APP_DIR" && runuser -u "$APP_USER" -- .venv/bin/story-watch --deploy-notify "$DEPLOY_COMMIT" \
        "${changelog[@]}" < "$log_file") \
        || echo "    deploy notification failed; the deploy itself succeeded" >&2
    [[ -z "${DEPLOY_SHA:-}" ]] || echo "$DEPLOY_SHA" > "$APP_DIR/deployed-commit"
fi
