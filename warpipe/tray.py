"""System tray icon (PySide6, QSystemTrayIcon — on Plasma Wayland via SNI).

Menu:
  Discord Go Live audio: active / not detected   (info)
  To Discord only: …                             (info)
  [x] Only selected apps
      Whole system (restore)
  Apps playing audio:
  [x] Warframe.x64.exe
  [ ] Firefox
  (Which Discord stream? — only when detection is ambiguous)
      Quit

Left click opens the same menu as right click (its first line is the status);
see menu_anchor() for where it opens.

Graph watching runs in a separate thread (Watcher); the state reaches the GUI
through a Qt signal (queued connection between threads).

Icon: data/icons/tray/warpipe-tray-{light,dark}[-active|-off].svg (installed as
<appdir>/icons/tray). "light" = dark glyph for light panels, "dark" = light glyph
for dark panels, chosen from the palette's text color. "-active" while enforcing
with a capture present, "-off" when enforcement is disabled ("Whole system").
Without those files: the theme icon io.github.gawson.Warpipe-symbolic, then a
generic audio icon.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QSize, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QGuiApplication, QIcon,
                           QImageReader, QPalette, QPixmap)
from PySide6.QtWidgets import (QApplication, QMenu, QMessageBox, QStyle,
                               QSystemTrayIcon)

from . import APP_ID, APP_NAME, DESKTOP_ID
from .control import start_control_server
from .enforcer import Enforcer, InvalidAppName, Status, Watcher
from .i18n import _, ngettext
from .planner import KIND_MIC, STATUS_AMBIGUOUS
from .state import Config, InstanceLock, SnapshotStore

log = logging.getLogger("warpipe")

ICON_STATE_NORMAL = ""
ICON_STATE_ACTIVE = "active"
ICON_STATE_OFF = "off"
ICON_SIZES = (16, 22, 24, 32, 48, 64)
THEME_ICON = DESKTOP_ID + "-symbolic"


# ------------------------------------------------------------------ icons
def tray_icons_dir(pkg_dir: Path | str | None = None) -> Path | None:
    """Directory with the tray SVGs, resolved relative to the package:
    <repo>/data/icons/tray in a checkout, <appdir>/icons/tray when installed."""
    pkg = Path(pkg_dir) if pkg_dir is not None else Path(__file__).resolve().parent
    base = pkg.parent
    for d in (base / "data" / "icons" / "tray", base / "icons" / "tray"):
        if d.is_dir():
            return d
    return None


def tray_icon_name(variant: str, state: str = ICON_STATE_NORMAL) -> str:
    return f"warpipe-tray-{variant}" + (f"-{state}" if state else "") + ".svg"


def tray_icon_files(variant: str, state: str = ICON_STATE_NORMAL,
                    icons_dir: Path | None = None) -> tuple[Path, Path | None] | None:
    """(main SVG, 16 px SVG or None) for a variant/state, or None if missing."""
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


def icon_state(st: Status) -> str:
    if not st.enabled:
        return ICON_STATE_OFF
    if st.detection is not None and st.detection.found:
        return ICON_STATE_ACTIVE
    return ICON_STATE_NORMAL


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


def _theme_icon(names: list[str], fallback: QStyle.StandardPixmap) -> QIcon:
    for n in names:
        icon = QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
    app = QApplication.instance()
    return app.style().standardIcon(fallback) if app else QIcon()


def load_tray_icon(variant: str, state: str, icons_dir: Path | None = None) -> QIcon:
    files = tray_icon_files(variant, state, icons_dir)
    if files is not None:
        icon = _svg_icon(*files)
        if not icon.isNull():
            return icon
        log.warning("could not load the tray icon %s — using the theme icon", files[0])
    names = ([f"{DESKTOP_ID}-{state}-symbolic"] if state else []) + [THEME_ICON, "audio-volume-high", "audio-card"]
    return _theme_icon(names, QStyle.StandardPixmap.SP_MediaVolume)


Rect = tuple[int, int, int, int]   # x, y, width, height


def _inside(p: tuple[int, int], r: Rect) -> bool:
    x, y, w, h = r
    return w > 0 and h > 0 and x <= p[0] < x + w and y <= p[1] < y + h


def menu_anchor(cursor: tuple[int, int] | None, screens: list[Rect], tray: Rect | None,
                primary_available: Rect | None) -> tuple[int, int]:
    """Where the menu opens on a left click (QMenu.popup then keeps it on screen).

    1. the mouse cursor — unless it is unusable: on Wayland QCursor.pos() is often
       (0, 0) or a stale point outside every screen;
    2. the centre of the tray icon, if Qt knows its geometry (often not on Wayland);
    3. the bottom-right corner of the primary screen's available area (where the
       system tray usually is).
    """
    if cursor is not None and tuple(cursor) != (0, 0) and any(_inside(cursor, s) for s in screens):
        return int(cursor[0]), int(cursor[1])
    if tray is not None and tray[2] > 0 and tray[3] > 0:
        return tray[0] + tray[2] // 2, tray[1] + tray[3] // 2
    if primary_available is not None and primary_available[2] > 0 and primary_available[3] > 0:
        x, y, w, h = primary_available
        return x + w - 1, y + h - 1
    return 0, 0


def _rect(r) -> Rect | None:
    return (r.x(), r.y(), r.width(), r.height()) if r is not None and r.isValid() else None


def _streams_suffix(n: int) -> str:
    if n <= 1:
        return ""
    return " (" + ngettext("{n} stream", "{n} streams", n).format(n=n) + ")"


class _Bridge(QObject):
    status = Signal(object)
    event = Signal(str, str)


class TrayController(QObject):
    def __init__(self, enforcer: Enforcer, watcher: Watcher | None = None) -> None:
        super().__init__()
        self.enforcer = enforcer
        self.watcher = watcher
        self._bridge = _Bridge()
        self._bridge.status.connect(self._on_status)
        self._bridge.event.connect(self._on_event)
        enforcer.on_status = self._bridge.status.emit
        enforcer.on_event = self._bridge.event.emit

        self._icons: dict[tuple[str, str], QIcon] = {}
        self.icon_key: tuple[str, str] | None = None
        hints = QGuiApplication.styleHints()
        scheme_changed = getattr(hints, "colorSchemeChanged", None) if hints is not None else None
        if scheme_changed is not None:
            scheme_changed.connect(self._on_theme_changed)

        self.menu = QMenu()
        self.menu.setToolTipsVisible(True)
        self.menu.aboutToShow.connect(self._on_about_to_show)
        self.tray = QSystemTrayIcon(self.icon_for(enforcer.status))
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self._signature = None
        self._pending_status: Status | None = None
        self._rebuild_scheduled = False
        self._last_msg: tuple[str, float] = ("", 0.0)
        self.app_actions: dict[str, QAction] = {}
        self.act_enforce: QAction | None = None
        self.act_restore: QAction | None = None
        self.act_quit: QAction | None = None
        self.build_menu(enforcer.status)

    # -------------------------------------------------------------- icon
    def icon_for(self, st: Status) -> QIcon:
        key = (palette_variant(), icon_state(st))
        self.icon_key = key
        icon = self._icons.get(key)
        if icon is None:
            icon = self._icons[key] = load_tray_icon(*key)
        return icon

    def _on_theme_changed(self, *_args) -> None:
        self._icons.clear()
        self._update_tray(self.enforcer.status)

    # -------------------------------------------------------------- menu
    def _menu_signature(self, st: Status) -> tuple:
        det = st.detection
        cands = tuple((c.serial, c.kind) for c in det.candidates) if det else ()
        apps = tuple((a.key, len(a.nodes), tuple(a.hints())) for a in st.apps)
        return (st.headline(), st.routing_text(), tuple(st.warnings), st.error, st.enabled,
                tuple(st.selected), apps, cands, st.capture_status,
                tuple(self.enforcer.override or ()))

    def build_menu(self, st: Status | None = None) -> None:
        st = st or self.enforcer.status
        sig = self._menu_signature(st)
        if sig == self._signature and self.act_quit is not None:
            return
        self._signature = sig
        m = self.menu
        m.clear()
        self.app_actions = {}

        head = m.addAction(st.headline())
        head.setEnabled(False)
        route = m.addAction(st.routing_text())
        route.setEnabled(False)
        msgs = list(st.warnings)
        if st.error:
            msgs.append(_("Error: {error}").format(error=st.error))
        for w in msgs:
            a = m.addAction(w if len(w) <= 90 else w[:89] + "…")
            a.setToolTip(w)
            a.setEnabled(False)
        m.addSeparator()

        self.act_enforce = m.addAction(_("Only selected apps"))
        self.act_enforce.setCheckable(True)
        self.act_enforce.setChecked(st.enabled)
        self.act_enforce.triggered.connect(self._on_toggle_enforce)
        self.act_restore = m.addAction(_("Whole system (restore)"))
        self.act_restore.triggered.connect(self._on_restore)
        m.addSeparator()

        label = m.addAction(_("Apps playing audio:"))
        label.setEnabled(False)
        selected_cf = {k.casefold() for k in st.selected}
        for a in st.apps:
            act = m.addAction(a.display + _streams_suffix(len(a.nodes)))
            act.setCheckable(True)
            act.setChecked(a.key.casefold() in selected_cf)
            hints = a.hints()
            if hints:
                # media.name only as a UI hint (never goes to logs or the config)
                act.setToolTip("\n".join(h if len(h) <= 80 else h[:79] + "…" for h in hints[:5]))
            act.triggered.connect(lambda checked, key=a.key: self._on_app(key, checked))
            self.app_actions[a.key] = act
        for key in st.missing_selected():
            act = m.addAction(_("{app} (not playing now)").format(app=key))
            act.setCheckable(True)
            act.setChecked(True)
            act.triggered.connect(lambda checked, key=key: self._on_app(key, checked))
            self.app_actions[key] = act
        if not st.apps and not st.missing_selected():
            none = m.addAction(_("(no app is playing right now)"))
            none.setEnabled(False)

        det = st.detection
        if det is not None and (det.status == STATUS_AMBIGUOUS or self.enforcer.override):
            sub = m.addMenu(_("Which Discord stream?"))
            auto = sub.addAction(_("Automatic"))
            auto.setCheckable(True)
            auto.setChecked(not self.enforcer.override)
            auto.triggered.connect(lambda _checked=False: self._on_pick_capture(None))
            for c in det.candidates:
                if c.kind == KIND_MIC:
                    continue
                warn = (" (" + _("check that it isn't a microphone") + ")"
                        if c.needs_confirm and not c.confirmed_by(self.enforcer.dcfg) else "")
                act = sub.addAction(f"#{c.id} {c.node.name or '?'} — {c.reasons_text(60)}{warn}")
                act.setCheckable(True)
                act.setChecked(bool(self.enforcer.override) and c.serial in self.enforcer.override)
                act.triggered.connect(lambda _checked=False, s=c.serial: self._on_pick_capture([s]))

        m.addSeparator()
        self.act_quit = m.addAction(_("Quit"))
        self.act_quit.triggered.connect(self._on_quit)
        self._update_tray(st)

    def _update_tray(self, st: Status) -> None:
        self.tray.setIcon(self.icon_for(st))
        tip = f"{APP_NAME}\n{st.headline()}\n{st.routing_text()}"
        if st.error:
            tip += "\n" + _("Error: {error}").format(error=st.error)
        self.tray.setToolTip(tip)

    # ------------------------------------------------------------ events
    def _on_status(self, st: Status) -> None:
        # Deferred: a status may arrive while a menu action's click is being handled,
        # and rebuilding deletes the actions — that must not happen inside their signal.
        self._pending_status = st
        if not getattr(self, "_rebuild_scheduled", False):
            self._rebuild_scheduled = True
            QTimer.singleShot(0, self._rebuild_pending)

    def _rebuild_pending(self) -> None:
        self._rebuild_scheduled = False
        st = getattr(self, "_pending_status", None)
        if st is not None:
            self.build_menu(st)

    def _on_event(self, kind: str, msg: str) -> None:
        if kind not in ("applied", "restored", "capture_found", "error", "loop", "warning"):
            return
        now = time.monotonic()
        if msg == self._last_msg[0] and now - self._last_msg[1] < 5.0:
            return
        self._last_msg = (msg, now)
        icon = (QSystemTrayIcon.MessageIcon.Warning if kind in ("error", "loop", "warning")
                else QSystemTrayIcon.MessageIcon.Information)
        if self.tray.isVisible():
            self.tray.showMessage(APP_NAME, msg, icon, 4000)

    def _on_about_to_show(self) -> None:
        # The app list is rebuilt whenever the menu opens.
        try:
            st = self.enforcer.refresh("menu")
        except Exception:  # pragma: no cover
            log.exception("refresh on menu open")
            return
        self.build_menu(st)

    def _on_activated(self, reason) -> None:
        # Left click: the same menu as right click (users expect a menu, not a notification;
        # the status is its first line). Right click is handled by the platform.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.popup_menu()

    def popup_menu(self) -> None:
        pos = QCursor.pos()
        primary = QGuiApplication.primaryScreen()
        x, y = menu_anchor((pos.x(), pos.y()),
                           [_rect(s.geometry()) or (0, 0, 0, 0) for s in QGuiApplication.screens()],
                           _rect(self.tray.geometry()),
                           _rect(primary.availableGeometry()) if primary is not None else None)
        self.menu.popup(QPoint(x, y))

    def _on_toggle_enforce(self, checked: bool) -> None:
        self.enforcer.set_enabled(bool(checked))

    def _on_restore(self) -> None:
        self.enforcer.set_enabled(False)

    def _on_app(self, key: str, checked: bool) -> None:
        try:
            self.enforcer.set_app_selected(key, bool(checked))
        except InvalidAppName as e:   # a name from the graph that can't be saved
            self._on_event("error", str(e))

    def _on_pick_capture(self, serials) -> None:
        self.enforcer.set_capture_override(serials)

    def _on_quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------ showing
    def show_when_available(self, timeout_s: float = 60.0) -> None:
        """At autostart the Plasma panel may not be up yet — wait for it."""
        deadline = time.monotonic() + timeout_s

        def attempt():
            if QSystemTrayIcon.isSystemTrayAvailable() or time.monotonic() >= deadline:
                self.tray.show()
            else:
                QTimer.singleShot(2000, attempt)

        attempt()


def run_tray(args, config: Config, pw) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(DESKTOP_ID)
    app.setWindowIcon(QIcon.fromTheme(DESKTOP_ID))
    app.setQuitOnLastWindowClosed(False)

    lock = InstanceLock()
    if not lock.acquire():
        QMessageBox.information(None, APP_NAME,
                                _("Warpipe is already running — its icon is in the system tray "
                                  "(next to the clock)."))
        return 4

    enforcer = Enforcer(pw, config, SnapshotStore())
    watcher = Watcher(enforcer, pw)
    ctl = TrayController(enforcer, watcher)
    # Control channel (OpenDeck plugin, --toggle): its own stdlib threads that only call
    # Enforcer methods (RLock). The state still reaches the GUI via the _Bridge signal
    # (queued connection), so Qt objects are only touched by the GUI thread.
    server = start_control_server(enforcer)

    def on_signal(signum, frame):
        log.info("signal %s — quitting", signum)
        app.quit()

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    # Python only handles signals when it gets control — wake it up every 0.5 s.
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(500)

    thread = threading.Thread(target=watcher.run, name="warpipe-watcher", daemon=True)
    ctl.show_when_available()
    thread.start()
    try:
        rc = app.exec()
    finally:
        if server is not None:
            server.stop()
        watcher.stop()
        thread.join(timeout=5)
        ok, msg = enforcer.shutdown()
        if not ok:
            log.warning("restore on exit failed: %s", msg)
        ctl.tray.hide()
        lock.release()
    return rc
