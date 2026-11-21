"""Ikona w zasobniku (PySide6, QSystemTrayIcon — pod Plasma Wayland przez SNI).

Menu:
  Stream z dźwiękiem: wykryty / nie wykryty      (informacja)
  Do Discorda tylko: …                           (informacja)
  [x] Tylko zaznaczone do Discorda
      Cały system (przywróć)
  Aplikacje z dźwiękiem:
  [x] Warframe.x64.exe
  [ ] Firefox
  (Który strumień Discorda? — tylko gdy wykrywanie jest niepewne)
      Zakończ

Obserwacja grafu działa w osobnym wątku (Watcher); stan przychodzi do GUI
sygnałem Qt (połączenie kolejkowane między wątkami).
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (QApplication, QMenu, QMessageBox, QStyle,
                               QSystemTrayIcon)

from . import APP_ID, APP_NAME
from .control import start_control_server
from .enforcer import Enforcer, Status, Watcher
from .planner import KIND_MIC, STATUS_AMBIGUOUS, STATUS_FOUND
from .state import Config, InstanceLock, SnapshotStore

log = logging.getLogger("warpipe")


def _theme_icon(names: list[str], fallback: QStyle.StandardPixmap) -> QIcon:
    for n in names:
        icon = QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
    app = QApplication.instance()
    return app.style().standardIcon(fallback) if app else QIcon()


def _streams_suffix(n: int) -> str:
    if n <= 1:
        return ""
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f" ({n} strumienie)"
    return f" ({n} strumieni)"


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

        self.icon_on = _theme_icon(["audio-volume-high", "audio-card"], QStyle.StandardPixmap.SP_MediaVolume)
        self.icon_off = _theme_icon(["audio-volume-medium", "audio-volume-high"], QStyle.StandardPixmap.SP_MediaVolume)

        self.menu = QMenu()
        self.menu.setToolTipsVisible(True)
        self.menu.aboutToShow.connect(self._on_about_to_show)
        self.tray = QSystemTrayIcon(self.icon_off)
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
            msgs.append("Błąd: " + st.error)
        for w in msgs:
            a = m.addAction(w if len(w) <= 90 else w[:89] + "…")
            a.setToolTip(w)
            a.setEnabled(False)
        m.addSeparator()

        self.act_enforce = m.addAction("Tylko zaznaczone do Discorda")
        self.act_enforce.setCheckable(True)
        self.act_enforce.setChecked(st.enabled)
        self.act_enforce.triggered.connect(self._on_toggle_enforce)
        self.act_restore = m.addAction("Cały system (przywróć)")
        self.act_restore.triggered.connect(self._on_restore)
        m.addSeparator()

        label = m.addAction("Aplikacje z dźwiękiem:")
        label.setEnabled(False)
        selected_cf = {k.casefold() for k in st.selected}
        for a in st.apps:
            act = m.addAction(a.display + _streams_suffix(len(a.nodes)))
            act.setCheckable(True)
            act.setChecked(a.key.casefold() in selected_cf)
            hints = a.hints()
            if hints:
                # media.name tylko jako podpowiedź w UI (nie trafia do logów ani konfiguracji)
                act.setToolTip("\n".join(h if len(h) <= 80 else h[:79] + "…" for h in hints[:5]))
            act.triggered.connect(lambda checked, key=a.key: self._on_app(key, checked))
            self.app_actions[a.key] = act
        for key in st.missing_selected():
            act = m.addAction(f"{key} (teraz nie gra)")
            act.setCheckable(True)
            act.setChecked(True)
            act.triggered.connect(lambda checked, key=key: self._on_app(key, checked))
            self.app_actions[key] = act
        if not st.apps and not st.missing_selected():
            none = m.addAction("(żadna aplikacja teraz nie gra)")
            none.setEnabled(False)

        det = st.detection
        if det is not None and (det.status == STATUS_AMBIGUOUS or self.enforcer.override):
            sub = m.addMenu("Który strumień Discorda?")
            auto = sub.addAction("Automatycznie")
            auto.setCheckable(True)
            auto.setChecked(not self.enforcer.override)
            auto.triggered.connect(lambda _=False: self._on_pick_capture(None))
            for c in det.candidates:
                if c.kind == KIND_MIC:
                    continue
                warn = (" (sprawdź, czy to nie mikrofon)"
                        if c.needs_confirm and not c.confirmed_by(self.enforcer.dcfg) else "")
                act = sub.addAction(f"#{c.id} {c.node.name or '?'} — {', '.join(c.reasons)[:60]}{warn}")
                act.setCheckable(True)
                act.setChecked(bool(self.enforcer.override) and c.serial in self.enforcer.override)
                act.triggered.connect(lambda _=False, s=c.serial: self._on_pick_capture([s]))

        m.addSeparator()
        self.act_quit = m.addAction("Zakończ")
        self.act_quit.triggered.connect(self._on_quit)
        self._update_tray(st)

    def _update_tray(self, st: Status) -> None:
        self.tray.setIcon(self.icon_on if st.enabled else self.icon_off)
        tip = f"{APP_NAME}\n{st.headline()}\n{st.routing_text()}"
        if st.error:
            tip += f"\nBłąd: {st.error}"
        self.tray.setToolTip(tip)

    # ------------------------------------------------------------ zdarzenia
    def _on_status(self, st: Status) -> None:
        # Odroczone: status może przyjść w trakcie obsługi kliknięcia w akcję menu,
        # a przebudowa usuwa akcje — nie wolno tego robić wewnątrz ich sygnału.
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
        if kind not in ("applied", "restored", "capture_found", "error", "loop"):
            return
        now = time.monotonic()
        if msg == self._last_msg[0] and now - self._last_msg[1] < 5.0:
            return
        self._last_msg = (msg, now)
        icon = (QSystemTrayIcon.MessageIcon.Warning if kind in ("error", "loop")
                else QSystemTrayIcon.MessageIcon.Information)
        if self.tray.isVisible():
            self.tray.showMessage(APP_NAME, msg, icon, 4000)

    def _on_about_to_show(self) -> None:
        # Lista aplikacji budowana od nowa przy otwarciu menu.
        try:
            st = self.enforcer.refresh("menu")
        except Exception:  # pragma: no cover
            log.exception("odświeżanie przy otwarciu menu")
            return
        self.build_menu(st)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            st = self.enforcer.status
            self.tray.showMessage(APP_NAME, f"{st.headline()}\n{st.routing_text()}",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)

    def _on_toggle_enforce(self, checked: bool) -> None:
        self.enforcer.set_enabled(bool(checked))

    def _on_restore(self) -> None:
        self.enforcer.set_enabled(False)

    def _on_app(self, key: str, checked: bool) -> None:
        self.enforcer.set_app_selected(key, bool(checked))

    def _on_pick_capture(self, serials) -> None:
        self.enforcer.set_capture_override(serials)

    def _on_quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------ pokazanie
    def show_when_available(self, timeout_s: float = 60.0) -> None:
        """Przy autostarcie panel Plasmy może jeszcze nie działać — czekamy na niego."""
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
    app.setDesktopFileName("warpipe")
    app.setQuitOnLastWindowClosed(False)

    lock = InstanceLock()
    if not lock.acquire():
        QMessageBox.information(None, APP_NAME,
                                "Warpipe już działa — jego ikona jest w zasobniku (obok zegara).")
        return 4

    enforcer = Enforcer(pw, config, SnapshotStore())
    watcher = Watcher(enforcer, pw)
    ctl = TrayController(enforcer, watcher)
    # Kanał sterowania (wtyczka OpenDeck, --toggle): własne wątki stdlib, które wołają
    # tylko metody Enforcera (RLock). Stan do GUI idzie dalej przez sygnał _Bridge
    # (połączenie kolejkowane), więc obiekty Qt dotyka wyłącznie wątek GUI.
    server = start_control_server(enforcer)

    def on_signal(signum, frame):
        log.info("sygnał %s — kończę", signum)
        app.quit()

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    # Python obsługuje sygnały tylko, gdy dostaje sterowanie — budzimy go co 0,5 s.
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
            log.warning("przywracanie przy wyjściu nieudane: %s", msg)
        ctl.tray.hide()
        lock.release()
    return rc
