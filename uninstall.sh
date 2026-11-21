#!/usr/bin/env bash
# Remove a user-level Warpipe installation made by install.sh.
# A running Warpipe is stopped with SIGTERM first: it restores Discord's original audio links on exit.
# Your settings in ~/.config/warpipe are kept unless you pass --purge.
set -euo pipefail

DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
APPDIR=$DATA/warpipe
MANIFEST=$APPDIR/installed-files.txt
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ ! -f "$MANIFEST" ]; then
  echo "No Warpipe installation found in $APPDIR" >&2
  exit 1
fi

# stop running instances of the installed copy (they restore links on SIGTERM)
pids=$(pgrep -u "$(id -u)" -f "$APPDIR/warpipe-launcher" || true)
if [ -n "$pids" ]; then
  echo "Stopping Warpipe ($pids)..."
  kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 50); do
    pgrep -u "$(id -u)" -f "$APPDIR/warpipe-launcher" >/dev/null || break
    sleep 0.1
  done
fi

# remove exactly what install.sh recorded (only paths inside expected locations)
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in
    "$APPDIR"/*|"$DATA"/icons/hicolor/*|"$DATA"/applications/*|"$CONF"/autostart/*|"$HOME"/.local/bin/warpipe|"${WARPIPE_BINDIR:-/nonexistent}"/warpipe) ;;
    *) echo "skipping unexpected path: $f" >&2; continue ;;
  esac
  rm -rf -- "$f"
done < "$MANIFEST"
rm -f -- "$MANIFEST"
rmdir "$APPDIR" 2>/dev/null || true

command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" 2>/dev/null || true

if [ "$PURGE" = 1 ]; then
  rm -rf -- "$CONF/warpipe"
  echo "Settings removed."
fi
echo "Warpipe uninstalled."
