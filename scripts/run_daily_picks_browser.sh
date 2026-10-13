#!/usr/bin/env bash
# Daily Chromium uses a local virtual display, independent of desktop login.
# Only the manual access window uses the existing server desktop.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/home/nik/.cache/ms-playwright}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$project_root/var/browser-picks/cache}"

for argument in "$@"; do
  if [[ "$argument" == --open-browser ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export TMPDIR="$project_root/var/browser-picks/tmp"
    mkdir -p -- "$TMPDIR"
    chmod 700 -- "$TMPDIR"
    while IFS= read -r entry; do
      case "$entry" in
        DISPLAY=*|XAUTHORITY=*|WAYLAND_DISPLAY=*) export "$entry" ;;
      esac
    done < <(systemctl --user show-environment)
    exec "$project_root/venv/bin/python" scripts/publish_daily_picks_browser.py --headed "$@"
  fi
done

export XDG_RUNTIME_DIR="$project_root/var/browser-picks/runtime"
mkdir -p -- "$XDG_RUNTIME_DIR"
chmod 700 -- "$XDG_RUNTIME_DIR"
exec xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' \
  "$project_root/venv/bin/python" scripts/publish_daily_picks_browser.py --headed "$@"
