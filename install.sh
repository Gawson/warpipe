#!/usr/bin/env bash
# Warpipe user-level installer (no root needed).
#   ./install.sh              install into ~/.local
#   ./install.sh --autostart  also start Warpipe at login
# Everything installed is listed in $APPDIR/installed-files.txt so uninstall.sh removes exactly that.
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
BINDIR=${WARPIPE_BINDIR:-$HOME/.local/bin}
APPDIR=$DATA/warpipe
APP_ID=io.github.gawson.Warpipe
AUTOSTART=0
for a in "$@"; do
  case "$a" in
    --autostart) AUTOSTART=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

warn() { echo "warning: $*" >&2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
for t in pw-dump pw-link; do command -v "$t" >/dev/null || warn "$t not found (install PipeWire tools)"; done
python3 -c 'import PySide6' 2>/dev/null || warn "PySide6 not found: the tray icon will not start (CLI still works). Fedora: sudo dnf install python3-pyside6"

MANIFEST=$APPDIR/installed-files.txt
mkdir -p "$APPDIR" "$BINDIR" "$DATA/applications"
: > "$MANIFEST.new"
track() { printf '%s\n' "$1" >> "$MANIFEST.new"; }

# application files
rm -rf "$APPDIR/warpipe" "$APPDIR/icons"
cp -r "$SRC/warpipe" "$APPDIR/warpipe"
find "$APPDIR/warpipe" -name '__pycache__' -prune -exec rm -rf {} +
cp "$SRC/bin/warpipe" "$APPDIR/warpipe-launcher"
chmod 755 "$APPDIR/warpipe-launcher"
mkdir -p "$APPDIR/icons" && cp -r "$SRC/data/icons/tray" "$APPDIR/icons/tray"
track "$APPDIR/warpipe"; track "$APPDIR/warpipe-launcher"; track "$APPDIR/icons"

# launcher on PATH (wrapper, so the package is found next to the real launcher)
cat > "$BINDIR/warpipe" <<EOF
#!/bin/sh
exec python3 "$APPDIR/warpipe-launcher" "\$@"
EOF
chmod 755 "$BINDIR/warpipe"; track "$BINDIR/warpipe"

# icons
while IFS= read -r -d '' f; do
  rel=${f#"$SRC/data/icons/hicolor/"}
  mkdir -p "$DATA/icons/hicolor/$(dirname "$rel")"
  cp "$f" "$DATA/icons/hicolor/$rel"; track "$DATA/icons/hicolor/$rel"
done < <(find "$SRC/data/icons/hicolor" -type f -print0)
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true

# desktop entry
desktop() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=Warpipe
GenericName=Stream audio picker
GenericName[pl]=Wybór dźwięku do streamu
Comment=Choose which apps are heard in your Discord Go Live stream
Comment[pl]=Wybierz, które aplikacje słychać w streamie Discord Go Live
Exec=$BINDIR/warpipe --tray
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Audio;Utility;
Keywords=Discord;Go Live;stream;PipeWire;audio;
StartupNotify=false
EOF
}
desktop > "$DATA/applications/$APP_ID.desktop"; track "$DATA/applications/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" 2>/dev/null || true

if [ "$AUTOSTART" = 1 ]; then
  mkdir -p "$CONF/autostart"
  { desktop; echo "X-KDE-autostart-phase=2"; } > "$CONF/autostart/$APP_ID.desktop"
  track "$CONF/autostart/$APP_ID.desktop"
fi

mv "$MANIFEST.new" "$MANIFEST"
echo "Warpipe installed."
echo "  start:     warpipe        (tray icon)    or   warpipe --list"
case ":$PATH:" in *":$BINDIR:"*) ;; *) warn "$BINDIR is not on your PATH; use $BINDIR/warpipe" ;; esac
echo "  uninstall: $SRC/uninstall.sh"
