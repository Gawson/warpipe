"""System tray icon (PySide6, QSystemTrayIcon — on Plasma Wayland via SNI).

Left click: the Warpipe window (popup.PopupWindow) — shown, or hidden again by the
next left click. Not a menu: on Wayland a menu popped up from a tray click has no
parent surface and is never shown, and Qt always exports ItemIsMenu=false, so
Plasma does not open the menu on a left click itself.

Right click: the menu (exported over DBusMenu, shown by the panel):
  Discord Go Live audio: active / not detected   (info)
  To Discord only: …                             (info)
  [x] Only selected apps
      Whole system (restore)
  Apps playing audio:
  [x] Warframe.x64.exe
  [ ] Firefox
  (Which Discord stream? — only when detection is ambiguous)
      Quit

The window's menu button: Which Discord stream? (when needed), Language, Start at
login, Quit.

Notifications: only while the window is closed, only for what needs attention and
was not just caused by the user (errors, the loop protection, an ambiguous stream,
Go Live with nothing selected — once per Go Live session); see should_notify().

Graph watching runs in a separate thread (Watcher); the state reaches the GUI
through a Qt signal (queued connection between threads) and updates the menu and
the open window there.

Icon: the green pipe (see icons.py) — faded waves while waiting for Go Live,
full waves while enforcing with a capture present, grey when enforcement is off
("Whole system"); falls back to the monochrome tray SVGs, then theme icons.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCursor, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_ID, APP_NAME, DESKTOP_ID, i18n
from .control import start_control_server
from .enforcer import Enforcer, InvalidAppName, Status, Watcher
from .i18n import _, ngettext
from .icons import (desktop_index, icon_state, load_tray_icon, palette_variant,
                    tray_icon_files, tray_icons_dir, variant_for_text_color)  # noqa: F401 (re-exported)
from .planner import KIND_MIC, STATUS_AMBIGUOUS
from .popup import PopupWindow, place_near, popup_model
from .state import (Config, InstanceLock, SnapshotStore, autostart_available,
                    autostart_enabled, set_autostart)

log = logging.getLogger("warpipe")

# ------------------------------------------------------------ notifications
# A tray notification only when the window is NOT open (it shows the state itself)
# and something happened that needs the user's attention and that they did not
# just cause themselves. Expected results of their own actions (ticking an app,
# "Only selected apps", "Whole system", the saved selection applied when Go Live
# starts) are silent — the log keeps them.
NOTIFY_ERROR = "error"                 # restore failed, PipeWire tools failing, can't save …
NOTIFY_LOOP = "loop"                   # something keeps relinking — enforcement paused
NOTIFY_AMBIGUOUS = "ambiguous"         # can't tell which Discord stream is Go Live: pick one
NOTIFY_SILENT_STREAM = "silent_stream"  # Go Live audio, "Only selected apps", nothing selected
ATTENTION = frozenset({NOTIFY_ERROR, NOTIFY_LOOP, NOTIFY_AMBIGUOUS, NOTIFY_SILENT_STREAM})
NOTIFY_INTERVAL = 60.0                 # the same text at most once a minute


def should_notify(kind: str, window_visible: bool, last_sent: float | None, now: float,
                  interval: float = NOTIFY_INTERVAL) -> bool:
    """Show a tray notification? kind: an Enforcer event kind or NOTIFY_*; last_sent:
    when the same text was shown last (None = never)."""
    if window_visible or kind not in ATTENTION:
        return False
    return last_sent is None or now - last_sent >= interval

LANGUAGE_CHOICES = (("auto", None), ("en", "English"), ("pl", "Polski"))   # names never translated


def _rect(r) -> tuple[int, int, int, int] | None:
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
        self._sent: dict[str, float] = {}        # notification text -> when shown
        self._was_ambiguous = False
        self._silent_noticed = False              # this Go Live session (see _status_alerts)
        self.app_actions: dict[str, QAction] = {}
        self.act_enforce: QAction | None = None
        self.act_restore: QAction | None = None
        self.act_quit: QAction | None = None
        self.act_autostart: QAction | None = None
        self.language_actions: dict[str, QAction] = {}
        self.build_menu(enforcer.status)

        self.popup = PopupWindow()
        self.popup.enforce_toggled.connect(self._on_toggle_enforce)
        self.popup.app_toggled.connect(self._on_app)
        self.popup.whole_system_clicked.connect(self._on_restore)
        self.settings_menu = QMenu(self.popup)
        self.settings_menu.setToolTipsVisible(True)
        self.settings_menu.aboutToShow.connect(self.build_settings_menu)
        self.popup.menu_button.setMenu(self.settings_menu)
        self.build_settings_menu()

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

        self._add_capture_menu(m, st)

        m.addSeparator()
        self.act_quit = m.addAction(_("Quit"))
        self.act_quit.triggered.connect(self._on_quit)
        self._update_tray(st)

    def _add_capture_menu(self, m: QMenu, st: Status) -> None:
        """"Which Discord stream?" — only when detection is ambiguous or overridden."""
        det = st.detection
        if det is None or not (det.status == STATUS_AMBIGUOUS or self.enforcer.override):
            return
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

    def build_settings_menu(self) -> None:
        """The window's menu button: stream choice (when needed), language, start at login, quit."""
        m = self.settings_menu
        m.clear()
        self._add_capture_menu(m, self.enforcer.status)
        lang = m.addMenu(_("Language"))
        group = QActionGroup(lang)
        current = self.enforcer.config.language
        self.language_actions: dict[str, QAction] = {}
        for value, name in LANGUAGE_CHOICES:
            act = lang.addAction(name or _("Automatic (system)"))
            act.setCheckable(True)
            act.setChecked(value == current)
            group.addAction(act)
            act.triggered.connect(lambda _checked=False, v=value: self.set_language(v))
            self.language_actions[value] = act
        self.act_autostart = m.addAction(_("Start at login"))
        self.act_autostart.setCheckable(True)
        on = autostart_enabled()
        self.act_autostart.setChecked(on)
        if not on and not autostart_available():
            self.act_autostart.setEnabled(False)
            self.act_autostart.setToolTip(_("Available after installing Warpipe (install.sh)"))
        self.act_autostart.triggered.connect(self._on_autostart)
        m.addSeparator()
        quit_ = m.addAction(_("Quit"))
        quit_.triggered.connect(self._on_quit)

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
            self._update_popup(st)
            self._status_alerts(st)

    def _on_event(self, kind: str, msg: str) -> None:
        # Enforcer events: only errors and the loop protection can notify; "applied",
        # "restored", "capture_found" and the empty-selection "warning" (a result of
        # the user's own click) are silent — see _status_alerts for a silent stream.
        self.notify(kind, msg)

    def notify(self, kind: str, msg: str) -> bool:
        now = time.monotonic()
        if not should_notify(kind, self.popup.isVisible(), self._sent.get(msg), now):
            return False
        if not self.tray.isVisible():
            return False
        self._sent[msg] = now
        icon = (QSystemTrayIcon.MessageIcon.Critical if kind == NOTIFY_ERROR
                else QSystemTrayIcon.MessageIcon.Warning)
        self.tray.showMessage(APP_NAME, msg, icon, 6000)
        return True

    def _status_alerts(self, st: Status) -> None:
        """Attention-worthy states seen in a status (not on every refresh):
        - detection became ambiguous (the user has to pick a stream);
        - Go Live audio while "Only selected apps" has nothing selected (the stream
          is silent) — at most once per Go Live session; while the window is open
          the window's notice counts as telling the user."""
        ambiguous = st.capture_status == STATUS_AMBIGUOUS
        if ambiguous and not self._was_ambiguous:
            self.notify(NOTIFY_AMBIGUOUS, st.headline())
        self._was_ambiguous = ambiguous
        found = st.detection is not None and st.detection.found
        if not found:
            self._silent_noticed = False          # Go Live ended: a new session may warn again
        elif st.enabled and not st.selected and not self._silent_noticed:
            self._silent_noticed = True
            self.notify(NOTIFY_SILENT_STREAM, _("No app selected — the stream has no audio until you tick one"))

    def _on_about_to_show(self) -> None:
        # The app list is rebuilt whenever the menu opens.
        try:
            st = self.enforcer.refresh("menu")
        except Exception:  # pragma: no cover
            log.exception("refresh on menu open")
            return
        self.build_menu(st)

    def _on_activated(self, reason) -> None:
        # Left click: the window (toggles). Right click: the menu, shown by the platform.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_popup()

    # ------------------------------------------------------------ window
    def _update_popup(self, st: Status, force: bool = False) -> None:
        # Only while it is shown — show_popup() refreshes it anyway.
        if self.popup.isVisible() or force:
            self.popup.set_model(popup_model(st, desktop_index()))

    def toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
        else:
            self.show_popup()

    def show_popup(self) -> None:
        try:
            st = self.enforcer.refresh("window")
        except Exception:  # pragma: no cover
            log.exception("refresh on window open")
            st = self.enforcer.status
        self._update_popup(st, force=True)
        if QGuiApplication.platformName() == "xcb":
            # X11 only: Wayland ignores positions, the compositor places the window.
            self._place_popup_x11()
        self.popup.show()
        self.popup.raise_()
        self.popup.activateWindow()

    def _place_popup_x11(self) -> None:
        pos = QCursor.pos()
        anchor = _rect(self.tray.geometry()) or (pos.x(), pos.y(), 1, 1)
        screen = (QGuiApplication.screenAt(self.tray.geometry().center()) or QGuiApplication.screenAt(pos)
                  or QGuiApplication.primaryScreen())
        if screen is None:
            return
        area = _rect(screen.availableGeometry())
        if area is None:
            return
        frame = self.popup.frameGeometry()
        w = max(frame.width(), self.popup.width())
        h = max(frame.height(), self.popup.height())
        x, y = place_near(w, h, anchor, area)
        self.popup.move(x, y)

    # ------------------------------------------------------------ settings
    def set_language(self, value: str) -> None:
        """"auto", "en" or "pl": saved, and the menu and the window switch right away."""
        self.enforcer.set_language(value)
        i18n.set_language(None if value == "auto" else value)
        try:
            st = self.enforcer.refresh("language")   # status texts in the new language
        except Exception:  # pragma: no cover
            log.exception("refresh after a language change")
            st = self.enforcer.status
        self._signature = None
        self.build_menu(st)
        self.popup.retranslate()
        self._update_popup(st)

    def _on_autostart(self, checked: bool) -> None:
        try:
            set_autostart(bool(checked))
        except OSError as e:
            self.notify(NOTIFY_ERROR, _("Can't change start at login: {error}").format(error=e))
        if self.act_autostart is not None:
            self.act_autostart.setChecked(autostart_enabled())

    def _on_toggle_enforce(self, checked: bool) -> None:
        self.enforcer.set_enabled(bool(checked))

    def _on_restore(self) -> None:
        self.enforcer.set_enabled(False)

    def _on_app(self, key: str, checked: bool) -> None:
        try:
            self.enforcer.set_app_selected(key, bool(checked))
        except InvalidAppName as e:   # a name from the graph that can't be saved
            self.notify(NOTIFY_ERROR, str(e))

    def _on_pick_capture(self, serials) -> None:
        self.enforcer.set_capture_override(serials)

    def _on_quit(self) -> None:
        self.popup.hide()
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
        ctl.popup.hide()
        ctl.tray.hide()
        lock.release()
    return rc
