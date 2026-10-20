#!/usr/bin/env bash
# Publication consumes local human browser receipts without launching a display.
# Server Chromium is retained only for explicit nonpublishing diagnostics.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/home/nik/.cache/ms-playwright}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$project_root/var/browser-picks/cache}"

source_handoff=false
browser_diagnostic=false
manual_browser=false
for argument in "$@"; do
  case "$argument" in
    --source-checks|--source-checks=*) source_handoff=true ;;
    --dry-run) browser_diagnostic=true ;;
    --open-browser) manual_browser=true ;;
  esac
done

if [[ "$source_handoff" == true || ( "$browser_diagnostic" == false && "$manual_browser" == false ) ]]; then
  # Includes the intentional no-handoff refusal: no Xvfb or browser is opened.
  exec "$project_root/venv/bin/python" scripts/publish_daily_picks_browser.py "$@"
fi

if [[ "$manual_browser" == true ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export TMPDIR="$project_root/var/browser-picks/tmp"
  mkdir -p -- "$TMPDIR"
  chmod 700 -- "$TMPDIR"
  while IFS= read -r entry; do
    case "$entry" in
      DISPLAY=*|XAUTHORITY=*|WAYLAND_DISPLAY=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_SESSION_TYPE=*|XDG_CURRENT_DESKTOP=*) export "$entry" ;;
    esac
  done < <(systemctl --user show-environment)
  exec "$project_root/venv/bin/python" scripts/publish_daily_picks_browser.py --headed "$@"
fi

export XDG_RUNTIME_DIR="$project_root/var/browser-picks/runtime"
mkdir -p -- "$XDG_RUNTIME_DIR"
chmod 700 -- "$XDG_RUNTIME_DIR"
exec xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' \
  "$project_root/venv/bin/python" scripts/publish_daily_picks_browser.py --headed "$@"
