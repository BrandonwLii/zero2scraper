# Shared helpers for refresh_session.sh (sourced, not run).
# Never prints cookie values.

# Sets $cookies from FIREFOX_COOKIES or the single default-release profile; returns 2 otherwise.
sw_find_cookies() {
  cookies="${FIREFOX_COOKIES:-}"
  if [ -z "$cookies" ]; then
    shopt -s nullglob
    local found=(/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*.default-release/cookies.sqlite)
    shopt -u nullglob
    if [ "${#found[@]}" -ne 1 ]; then
      echo "found ${#found[@]} Firefox cookie databases; set FIREFOX_COOKIES=/path/to/cookies.sqlite" >&2
      return 2
    fi
    cookies="${found[0]}"
  fi
  [ -f "$cookies" ] || { echo "no such file: FIREFOX_COOKIES" >&2; return 2; }
}

# True if the LAN alias answers (plain sshd, key-only; works only at home).
sw_lan_ok() { timeout 8 ssh -o BatchMode=yes pve-lan true >/dev/null 2>&1; }

IG_USER_CACHE="$HOME/.config/story-watch/ig_user"
