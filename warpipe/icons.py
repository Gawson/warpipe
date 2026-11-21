"""Icons: the green pipe (tray icon, window header), its fallbacks, and app icons.

Tray icon states (icon_state()):
  ""        enforcing, no Go Live audio yet — green pipe, faded waves
  "active"  enforcing with a Discord capture present — green pipe, full waves
  "off"     enforcement disabled ("Whole system") — the same pipe in grey
The green pipe is the app icon (data/icons/hicolor/<size>/apps, pixel-fitted at
16/22/24/32 px; installed as <appdir>/icons/hicolor). Faded waves and grey are
made at runtime. Fallback chain when those files are missing:
  1. the monochrome tray SVGs data/icons/tray/warpipe-tray-{light,dark}[-active|-off].svg
     (<appdir>/icons/tray); "light" = dark glyph for light panels, chosen from the
     palette's text color;
  2. the theme icon io.github.gawson.Warpipe[-state]-symbolic;
  3. a generic audio icon.

App icons (window rows): app_icon_names() lists theme icon names to try, best
first — PipeWire's application.icon-name, the desktop entry (.desktop Icon=) that
matches the Flatpak app id / binary / app name, the names themselves, a games
icon for Wine/Proton/Java programs, then generic icons.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QImageReader, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QStyle

from . import DESKTOP_ID
from .planner import _is_generic_loader

log = logging.getLogger("warpipe")

ICON_STATE_NORMAL = ""
ICON_STATE_ACTIVE = "active"
ICON_STATE_OFF = "off"
ICON_SIZES = (16, 22, 24, 32, 48, 64)
THEME_ICON = DESKTOP_ID + "-symbolic"
APP_ICON_FILE = DESKTOP_ID + ".svg"
FADED_WAVES = 0.35


def icon_state(st) -> str:
    """Tray icon state for an enforcer Status."""
    if not st.enabled:
        return ICON_STATE_OFF
    if st.detection is not None and st.detection.found:
        return ICON_STATE_ACTIVE
    return ICON_STATE_NORMAL


# ------------------------------------------------------------ data files
def _icons_subdir(sub: str, pkg_dir: Path | str | None = None) -> Path | None:
    """<repo>/data/icons/<sub> in a checkout, <appdir>/icons/<sub> when installed."""
    pkg = Path(pkg_dir) if pkg_dir is not None else Path(__file__).resolve().parent
    base = pkg.parent
    for d in (base / "data" / "icons" / sub, base / "icons" / sub):
        if d.is_dir():
            return d
    return None


def tray_icons_dir(pkg_dir: Path | str | None = None) -> Path | None:
    """Directory with the monochrome tray SVGs."""
    return _icons_subdir("tray", pkg_dir)


def app_icons_dir(pkg_dir: Path | str | None = None) -> Path | None:
    """hicolor directory with the green app icon SVGs."""
    return _icons_subdir("hicolor", pkg_dir)


def app_icon_file(size: int, hicolor: Path | None = None) -> Path | None:
    """The pixel-fitted app icon SVG for this size, else the scalable one."""
    d = hicolor if hicolor is not None else app_icons_dir()
    if d is None:
        return None
    for sub in (f"{size}x{size}", "scalable"):
        f = d / sub / "apps" / APP_ICON_FILE
        if f.is_file():
            return f
    return None


def tray_icon_name(variant: str, state: str = ICON_STATE_NORMAL) -> str:
    return f"warpipe-tray-{variant}" + (f"-{state}" if state else "") + ".svg"


def tray_icon_files(variant: str, state: str = ICON_STATE_NORMAL,
                    icons_dir: Path | None = None) -> tuple[Path, Path | None] | None:
    """(main SVG, 16 px SVG or None) of a monochrome variant/state, or None if missing."""
    d = icons_dir if icons_dir is not None else tray_icons_dir()
    if d is None:
        return None
    name = tray_icon_name(variant, state)
    main = d / name
    if not main.is_file():
        return None
    small = d / "16" / name
    return main, (small if small.is_file() else None)


def variant_for_text_color(color: QColor) -> str:
    """Light text = dark panel → the "dark" set (light glyph), and vice versa."""
    return "dark" if color.lightnessF() > 0.5 else "light"


def palette_variant(app: QGuiApplication | None = None) -> str:
    app = app or QGuiApplication.instance()
    if app is None:
        return "light"
    return variant_for_text_color(app.palette().color(QPalette.ColorRole.WindowText))


# ------------------------------------------------------------ green pipe
_WAVE_PATH = re.compile(r'<path(?=[^>]*\bfill="none")')


def fade_waves(svg: str, opacity: float = FADED_WAVES) -> str:
    """Fade the sound waves of an app icon SVG: in the pixel-fitted files they are the
    only unfilled path; in the scalable one they are the group id="waves"."""
    op = f' opacity="{opacity:g}"'
    svg = svg.replace('<g id="waves"', '<g id="waves"' + op)
    return _WAVE_PATH.sub("<path" + op, svg)


def _render_svg(data: bytes, size: int) -> QImage:
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf, b"svg")
    reader.setScaledSize(QSize(size, size))
    return reader.read()


def greyed(img: QImage) -> QImage:
    """The same image without color (alpha kept)."""
    src = img.convertToFormat(QImage.Format.Format_ARGB32)
    grey = src.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
    grey.setAlphaChannel(src.convertToFormat(QImage.Format.Format_Alpha8))
    return grey


def pipe_image(size: int, state: str = ICON_STATE_ACTIVE, hicolor: Path | None = None) -> QImage | None:
    f = app_icon_file(size, hicolor)
    if f is None:
        return None
    try:
        svg = f.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if state != ICON_STATE_ACTIVE:
        svg = fade_waves(svg)
    img = _render_svg(svg.encode("utf-8"), size)
    if img.isNull():
        return None
    return greyed(img) if state == ICON_STATE_OFF else img


def pipe_icon(state: str = ICON_STATE_ACTIVE, hicolor: Path | None = None) -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        img = pipe_image(size, state, hicolor)
        if img is not None:
            icon.addPixmap(QPixmap.fromImage(img))
    return icon


def pipe_pixmap(size: int, state: str = ICON_STATE_ACTIVE, dpr: float = 1.0,
                hicolor: Path | None = None) -> QPixmap:
    """The green pipe at `size` logical pixels (sharp on HiDPI)."""
    img = pipe_image(max(1, round(size * dpr)), state, hicolor)
    if img is None:
        return QPixmap()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


# ------------------------------------------------------------ tray icon
def _svg_icon(main: Path, small: Path | None) -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        src = small if (small is not None and size <= 16) else main
        reader = QImageReader(str(src))
        reader.setScaledSize(QSize(size, size))
        img = reader.read()
        if not img.isNull():
            icon.addPixmap(QPixmap.fromImage(img))
    return icon


def theme_icon(names: Iterable[str], fallback: QStyle.StandardPixmap | None = None) -> QIcon:
    for n in names:
        icon = QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
    app = QApplication.instance()
    return app.style().standardIcon(fallback) if (app and fallback is not None) else QIcon()


def load_tray_icon(variant: str, state: str, icons_dir: Path | None = None,
                   hicolor: Path | None = None) -> QIcon:
    icon = pipe_icon(state, hicolor)
    if not icon.isNull():
        return icon
    files = tray_icon_files(variant, state, icons_dir)
    if files is not None:
        icon = _svg_icon(*files)
        if not icon.isNull():
            return icon
        log.warning("could not load the tray icon %s — using the theme icon", files[0])
    names = ([f"{DESKTOP_ID}-{state}-symbolic"] if state else []) + [THEME_ICON, "audio-volume-high", "audio-card"]
    return theme_icon(names, QStyle.StandardPixmap.SP_MediaVolume)


# ------------------------------------------------------------ app icons
GAMES_ICON = "applications-games"
GENERIC_APP_ICONS = ("audio-x-generic", "application-x-executable")
ICON_PROPS = ("application.icon-name", "media.icon-name")
ID_PROPS = ("pipewire.access.portal.app_id", "application.id",
            "application.process.binary", "application.name")
_NOT_AN_APP = {"sh", "bash", "env", "flatpak", "gtk-launch", "kioclient", "kioclient5", "xdg-open"}


def _base(v: str) -> str:
    return v.strip().rsplit("/", 1)[-1]


def _is_game_loader(name: str) -> bool:
    b = _base(name).casefold()
    return b.endswith(".exe") or b.startswith("wine") or b.startswith("proton") or b in ("java", "javaw")


class DesktopIndex:
    """Icon= of installed desktop entries, looked up by desktop id, its last part
    (org.mozilla.firefox → firefox), StartupWMClass or the Exec binary (Flatpak:
    the app id after `flatpak run`). Read lazily, once."""

    def __init__(self, dirs: Iterable[Path] | None = None) -> None:
        self._dirs = list(dirs) if dirs is not None else None
        self._map: dict[str, str] | None = None

    @staticmethod
    def default_dirs() -> list[Path]:
        home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        rest = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
        return [Path(d) / "applications" for d in [home, *rest] if d]

    @staticmethod
    def _parse(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        in_entry = False
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("["):
                        if in_entry:
                            break
                        in_entry = line == "[Desktop Entry]"
                    elif in_entry and "=" in line:
                        k, v = line.split("=", 1)
                        out.setdefault(k.strip(), v.strip())
        except OSError:
            return {}
        return out

    @staticmethod
    def _exec_names(cmd: str) -> list[str]:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return []
        argv = [a for a in argv if not (a.startswith("%") or ("=" in a and not a.startswith("-")))]
        if argv and _base(argv[0]) == "env":
            argv = argv[1:]
        if not argv:
            return []
        names = [_base(argv[0])]
        if names[0] == "flatpak" and "run" in argv:
            ids = [a for a in argv[argv.index("run") + 1:] if not a.startswith("-")]
            names += ids[:1]
        return names

    def _load(self) -> dict[str, str]:
        m: dict[str, str] = {}
        for d in (self._dirs if self._dirs is not None else self.default_dirs()):
            try:
                files = sorted(d.glob("*.desktop")) if d.is_dir() else []
            except OSError:
                continue
            for f in files:
                e = self._parse(f)
                icon = e.get("Icon")
                if not icon or e.get("Hidden", "").lower() == "true":
                    continue
                keys = [f.stem, f.stem.rsplit(".", 1)[-1], e.get("StartupWMClass", "")]
                keys += self._exec_names(e.get("Exec", ""))
                for k in keys:
                    if k and k not in _NOT_AN_APP and not _is_generic_loader(k):
                        m.setdefault(k.casefold(), icon)
        return m

    def icon_for(self, name: str) -> str | None:
        if self._map is None:
            self._map = self._load()
        return self._map.get(name.strip().casefold()) if name else None


_desktop_index: DesktopIndex | None = None


def desktop_index() -> DesktopIndex:
    global _desktop_index
    if _desktop_index is None:
        _desktop_index = DesktopIndex()
    return _desktop_index


def app_icon_names(props_list: Iterable[dict], key: str, desktop: DesktopIndex | None = None) -> list[str]:
    """Theme icon names (or absolute paths) to try for an app, best first."""
    out: list[str] = []

    def add(v) -> None:
        if isinstance(v, str):
            v = v.strip()
            if v and v not in out:
                out.append(v)

    props_list = [p for p in props_list if isinstance(p, dict)]
    for props in props_list:
        for k in ICON_PROPS:
            add(props.get(k))
    ids: list[str] = []
    for props in props_list:
        for k in ID_PROPS:
            v = props.get(k)
            if isinstance(v, str) and v.strip() and _base(v) not in ids:
                ids.append(_base(v))
    if key and key not in ids:
        ids.append(key)
    names = [i for i in ids if not _is_generic_loader(i) and not _is_game_loader(i)]
    if desktop is not None:
        for i in names:
            add(desktop.icon_for(i))
    for i in names:
        add(i)
        add(i.casefold())
        add(i.casefold().replace(" ", "-"))
    if any(_is_game_loader(i) for i in ids):
        add(GAMES_ICON)
    for g in GENERIC_APP_ICONS:
        add(g)
    return out


def resolve_icon(names: Iterable[str]) -> QIcon:
    """The first of `names` that exists (theme name or absolute file path)."""
    for n in names:
        if n.startswith("/"):
            icon = QIcon(n) if Path(n).is_file() else QIcon()
        else:
            icon = QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
    return QIcon()
