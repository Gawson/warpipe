"""Tray icon without showing anything (QT_QPA_PLATFORM=offscreen)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

from helpers import ROOT, FakePw, GraphBuilder
from scenarios import base_system, monitor_mode, per_app, with_apps

from warpipe import i18n
from warpipe.enforcer import Enforcer, Status
from warpipe.state import Config, SnapshotStore


@unittest.skipUnless(HAVE_QT, "PySide6 missing")
class TestTrayIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def test_icons_dir_in_checkout(self):
        from warpipe.tray import tray_icons_dir
        self.assertEqual(tray_icons_dir(), Path(ROOT) / "data" / "icons" / "tray")

    def test_icons_dir_when_installed(self):
        """install.sh: <appdir>/warpipe (package) + <appdir>/icons/tray."""
        from warpipe.tray import tray_icon_files, tray_icons_dir
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "warpipe-app"
            (app / "warpipe").mkdir(parents=True)
            self.assertIsNone(tray_icons_dir(app / "warpipe"))
            (app / "icons" / "tray" / "16").mkdir(parents=True)
            self.assertEqual(tray_icons_dir(app / "warpipe"), app / "icons" / "tray")
            icons = app / "icons" / "tray"
            self.assertIsNone(tray_icon_files("dark", "off", icons))
            (icons / "warpipe-tray-dark-off.svg").write_text("<svg/>")
            self.assertEqual(tray_icon_files("dark", "off", icons), (icons / "warpipe-tray-dark-off.svg", None))
            (icons / "16" / "warpipe-tray-dark-off.svg").write_text("<svg/>")
            self.assertEqual(tray_icon_files("dark", "off", icons)[1], icons / "16" / "warpipe-tray-dark-off.svg")

    def test_all_variants_present_and_loadable(self):
        """The monochrome set: the fallback when the green app icon SVGs are missing."""
        from PySide6.QtCore import QSize

        from warpipe.tray import load_tray_icon, tray_icon_files
        with tempfile.TemporaryDirectory() as no_hicolor:
            for variant in ("light", "dark"):
                for state in ("", "active", "off"):
                    files = tray_icon_files(variant, state)
                    self.assertIsNotNone(files, (variant, state))
                    self.assertIsNotNone(files[1], (variant, state))   # 16 px version
                    icon = load_tray_icon(variant, state, hicolor=Path(no_hicolor))
                    self.assertFalse(icon.isNull())
                    sizes = icon.availableSizes()
                    self.assertIn(QSize(16, 16), sizes)
                    self.assertIn(QSize(22, 22), sizes)

    def test_fallback_without_files(self):
        from warpipe.tray import load_tray_icon
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(load_tray_icon("light", "active", Path(d), hicolor=Path(d)).isNull())

    def test_variant_from_text_color(self):
        from PySide6.QtGui import QColor

        from warpipe.tray import variant_for_text_color
        self.assertEqual(variant_for_text_color(QColor("#fcfcfc")), "dark")   # light text = dark panel
        self.assertEqual(variant_for_text_color(QColor("#232629")), "light")

    def test_icon_state(self):
        from warpipe.planner import STATUS_FOUND, STATUS_NONE, Detection
        from warpipe.tray import icon_state
        found = Detection(STATUS_FOUND, group=[object()])
        self.assertEqual(icon_state(Status(detection=found, enabled=False)), "off")
        self.assertEqual(icon_state(Status(detection=found, enabled=True)), "active")
        self.assertEqual(icon_state(Status(detection=Detection(STATUS_NONE), enabled=True)), "")
        self.assertEqual(icon_state(Status(detection=None, enabled=True)), "")


@unittest.skipUnless(HAVE_QT, "PySide6 missing")
class TestShouldNotify(unittest.TestCase):
    def test_policy(self):
        from warpipe.tray import should_notify
        for kind in ("error", "loop", "ambiguous", "silent_stream"):
            self.assertTrue(should_notify(kind, False, None, 100.0), kind)
            self.assertFalse(should_notify(kind, True, None, 100.0), kind)        # window open
        for kind in ("applied", "restored", "capture_found", "warning", "", "whatever"):
            self.assertFalse(should_notify(kind, False, None, 100.0), kind)      # expected results
        self.assertFalse(should_notify("error", False, 50.0, 109.9))              # same text < 60 s
        self.assertTrue(should_notify("error", False, 50.0, 110.0))
        self.assertTrue(should_notify("error", False, 50.0, 60.0, interval=10.0))


@unittest.skipUnless(HAVE_QT, "PySide6 missing")
class TestTrayMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.b, self.ns = per_app()
        self.fake = FakePw(self.b)
        self.enf = Enforcer(self.fake, Config(d / "config.json"), SnapshotStore(d / "snap.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def texts(self, menu):
        return [a.text() for a in menu.actions() if not a.isSeparator()]

    def test_menu_contents_and_toggle(self):
        from warpipe.tray import TrayController
        self.enf.refresh()
        ctl = TrayController(self.enf)
        texts = self.texts(ctl.menu)
        self.assertEqual(texts[0], "Discord Go Live audio: active")
        self.assertIn("Only selected apps", texts)
        self.assertIn("Whole system (restore)", texts)
        self.assertIn("Quit", texts)
        self.assertEqual(ctl.icon_key[1], "off")          # enforcement disabled
        self.assertEqual(set(ctl.app_actions), {"Firefox", "java", "Warframe.x64.exe"})
        self.assertNotIn("WEBRTC VoiceEngine", " ".join(texts))
        # media.name tylko jako podpowiedź
        self.assertIn("SEKRETNY", ctl.app_actions["Firefox"].toolTip())
        self.assertNotIn("SEKRETNY", " ".join(texts))
        # zaznaczenie aplikacji = włączenie pilnowania
        ctl.app_actions["Warframe.x64.exe"].trigger()
        self.app.processEvents()
        self.assertTrue(self.enf.enabled)
        self.assertTrue(ctl.act_enforce.isChecked())
        self.assertTrue(ctl.app_actions["Warframe.x64.exe"].isChecked())
        self.assertEqual(ctl.icon_key[1], "active")       # enforcing with a capture present
        # "Whole system"
        ctl.act_restore.trigger()
        self.app.processEvents()
        self.assertFalse(self.enf.enabled)
        self.assertFalse(ctl.act_enforce.isChecked())
        self.assertEqual(ctl.icon_key[1], "off")

    def test_menu_in_polish(self):
        from warpipe.tray import TrayController
        i18n.set_language("pl")
        try:
            self.enf.refresh()
            ctl = TrayController(self.enf)
            texts = self.texts(ctl.menu)
        finally:
            i18n.set_language("en")
        self.assertEqual(texts[0], "Stream z dźwiękiem: wykryty")
        self.assertIn("Tylko zaznaczone do Discorda", texts)
        self.assertIn("Cały system (przywróć)", texts)
        self.assertIn("Zakończ", texts)

    def test_empty_selection_warns(self):
        """Enforcing with nothing ticked keeps its meaning (no audio) but is never silent."""
        from PySide6.QtWidgets import QSystemTrayIcon

        from warpipe.tray import TrayController
        events = []
        self.enf.refresh()
        ctl = TrayController(self.enf)
        self.enf.on_event = lambda k, m: events.append((k, m))
        st = self.enf.set_enabled(True)
        self.assertTrue(self.enf.enabled)
        self.assertEqual(self.enf.selected, [])
        self.assertIn(("warning", "No app selected — the stream has no audio until you tick one"), events)
        self.assertEqual(st.routing_text(), "To Discord: nothing (stream audio muted)")
        self.assertEqual(self.b.links_into(self.ns.cap_ff), set())   # semantics unchanged: nothing to Discord
        self.assertEqual(self.b.links_into(self.ns.cap_wf), set())
        # the user's own click: no notification for the event itself …
        with mock.patch.object(ctl.tray, "isVisible", return_value=True), \
                mock.patch.object(ctl.tray, "showMessage") as show:
            ctl._on_event("warning", events[-1][1])
            show.assert_not_called()
            # … but a silent Go Live stream is reported once (window closed)
            ctl._status_alerts(st)
            ctl._status_alerts(self.enf.refresh())
        show.assert_called_once()
        self.assertEqual(show.call_args[0][1], "No app selected — the stream has no audio until you tick one")
        self.assertEqual(show.call_args[0][2], QSystemTrayIcon.MessageIcon.Warning)
        # ticking an app, then unticking it again while enforcing: warned again
        events.clear()
        self.enf.set_app_selected("Firefox", True)
        self.assertEqual(events[-1][0], "applied")
        self.enf.set_app_selected("Firefox", False)
        self.assertEqual(events[-1][0], "warning")
        i18n.set_language("pl")
        try:
            self.assertEqual(self.enf.status.routing_text(), "Do Discorda: nic (dźwięk streamu wyciszony)")
        finally:
            i18n.set_language("en")

    def test_notifications_only_for_unexpected_events(self):
        from PySide6.QtWidgets import QSystemTrayIcon

        from warpipe.tray import TrayController
        self.enf.refresh()
        ctl = TrayController(self.enf)
        now = [1000.0]
        with mock.patch.object(ctl.tray, "isVisible", return_value=True), \
                mock.patch.object(ctl.tray, "showMessage") as show, \
                mock.patch("warpipe.tray.time.monotonic", side_effect=lambda: now[0]):
            # the user's own actions: silent (Go Live active, an app ticked, Whole system)
            ctl.app_actions["Warframe.x64.exe"].trigger()
            self.app.processEvents()
            ctl.act_restore.trigger()
            self.app.processEvents()
            ctl.act_enforce.trigger()
            self.app.processEvents()
            for kind in ("applied", "restored", "capture_found", "warning"):
                ctl._on_event(kind, "something")
            show.assert_not_called()
            # errors: shown, the same text at most once a minute
            ctl._on_event("error", "pw-link failed")
            ctl._on_event("error", "pw-link failed")
            now[0] += 59
            ctl._on_event("error", "pw-link failed")
            self.assertEqual(show.call_count, 1)
            self.assertEqual(show.call_args[0][2], QSystemTrayIcon.MessageIcon.Critical)
            ctl._on_event("loop", "keeps relinking")
            self.assertEqual(show.call_count, 2)
            now[0] += 2
            ctl._on_event("error", "pw-link failed")
            self.assertEqual(show.call_count, 3)
            # while the window is open: nothing at all
            ctl.show_popup()
            now[0] += 600
            ctl._on_event("error", "pw-link failed")
            ctl._on_event("loop", "keeps relinking")
            self.assertEqual(show.call_count, 3)
            ctl.popup.hide()

    def test_silent_stream_once_per_go_live_session(self):
        from warpipe.tray import TrayController
        enf = Enforcer(self.fake, Config(Path(self.tmp.name) / "c4.json"),
                       SnapshotStore(Path(self.tmp.name) / "s4.json"), enabled=True, selected=[])
        ctl = TrayController(enf)
        now = [1000.0]
        with mock.patch.object(ctl.tray, "isVisible", return_value=True), \
                mock.patch.object(ctl.tray, "showMessage") as show, \
                mock.patch("warpipe.tray.time.monotonic", side_effect=lambda: now[0]):
            for _i in range(3):                         # every refresh of the same session
                ctl._status_alerts(enf.refresh())
            self.assertEqual(show.call_count, 1)
            b2, ns2 = self.b, self.ns
            b2.remove_node(ns2.cap_wf)                  # Go Live ends …
            b2.remove_node(ns2.cap_ff)
            ctl._status_alerts(enf.refresh())
            now[0] += 120
            cap = b2.capture(target=ns2.firefox.serial)  # … and starts again
            b2.connect(ns2.firefox, cap)
            ctl._status_alerts(enf.refresh())
            self.assertEqual(show.call_count, 2)
            # seen in the open window: that counts, no notification later in the session
            b2.remove_node(cap)
            ctl._status_alerts(enf.refresh())
            now[0] += 120
            cap = b2.capture(target=ns2.firefox.serial)
            b2.connect(ns2.firefox, cap)
            ctl.show_popup()
            ctl._status_alerts(enf.refresh())
            ctl.popup.hide()
            ctl._status_alerts(enf.refresh())
            self.assertEqual(show.call_count, 2)

    def test_ambiguous_detection_notifies_once(self):
        from warpipe.tray import TrayController
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        for _i in range(2):
            c = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
            b.connect(ns.alsa, c, src_prefix="monitor")
        enf = Enforcer(FakePw(b), Config(Path(self.tmp.name) / "c5.json"),
                       SnapshotStore(Path(self.tmp.name) / "s5.json"))
        ctl = TrayController(enf)
        with mock.patch.object(ctl.tray, "isVisible", return_value=True), \
                mock.patch.object(ctl.tray, "showMessage") as show:
            ctl._status_alerts(enf.refresh())
            ctl._status_alerts(enf.refresh())
        show.assert_called_once()
        self.assertIn("ambiguous", show.call_args[0][1])

    def test_about_to_show_rebuilds(self):
        from warpipe.tray import TrayController
        ctl = TrayController(self.enf)
        newapp = self.b.app("Spotify", "spotify")
        ctl.menu.aboutToShow.emit()
        self.app.processEvents()
        self.assertIn("Spotify", ctl.app_actions)

    def test_ambiguous_submenu(self):
        from warpipe.tray import TrayController
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        c1 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        c2 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, c1, src_prefix="monitor")
        b.connect(ns.alsa, c2, src_prefix="monitor")
        enf = Enforcer(FakePw(b), Config(Path(self.tmp.name) / "c2.json"), SnapshotStore(Path(self.tmp.name) / "s2.json"))
        enf.refresh()
        ctl = TrayController(enf)
        subs = [a.menu() for a in ctl.menu.actions() if a.menu() is not None]
        self.assertEqual(len(subs), 1)
        sub_texts = self.texts(subs[0])
        self.assertEqual(sub_texts[0], "Automatic")
        self.assertEqual(len(sub_texts), 3)

    def test_control_channel_updates_menu_from_other_thread(self):
        """Kanał sterowania działa w wątkach stdlib; menu (Qt) zmienia się tylko w wątku GUI
        przez kolejkowany sygnał — tak jak przy stanie z wątku pilnowania."""
        import time

        from warpipe.control import request, start_control_server
        from warpipe.tray import TrayController
        self.enf.refresh()
        ctl = TrayController(self.enf)
        path = Path(self.tmp.name) / "run" / "control.sock"
        srv = start_control_server(self.enf, path)
        self.assertIsNotNone(srv)
        try:
            r = request({"cmd": "toggle", "app": "Firefox"}, path)
            self.assertTrue(r["ok"], r)
            self.assertFalse(ctl.app_actions["Firefox"].isChecked())   # jeszcze nie w wątku GUI
            end = time.monotonic() + 3
            while not ctl.app_actions["Firefox"].isChecked() and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertTrue(ctl.app_actions["Firefox"].isChecked())
            self.assertTrue(ctl.act_enforce.isChecked())
            r = request({"cmd": "system"}, path)
            end = time.monotonic() + 3
            while ctl.act_enforce.isChecked() and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(ctl.act_enforce.isChecked())
        finally:
            srv.stop()
        self.assertFalse(path.exists())

    def test_monitor_only_capture_confirmed_from_menu(self):
        """Jedyny strumień „z monitora”: menu prosi o potwierdzenie; wybór zostaje zapamiętany."""
        import json
        from warpipe.tray import TrayController
        b, ns = monitor_mode("ee_sink")
        cfg_path = Path(self.tmp.name) / "c3.json"
        enf = Enforcer(FakePw(b), Config(cfg_path), SnapshotStore(Path(self.tmp.name) / "s3.json"),
                       enabled=True, selected=["Firefox"])
        st = enf.refresh()
        self.assertEqual(st.capture_status, "ambiguous")
        ctl = TrayController(enf)
        sub = next(a.menu() for a in ctl.menu.actions() if a.menu() is not None)
        pick = next(a for a in sub.actions() if a.text().startswith(f"#{ns.cap.id} "))
        self.assertIn("microphone", pick.text())
        pick.trigger()
        self.app.processEvents()
        self.assertEqual(enf.status.capture_status, "found")
        self.assertTrue(json.loads(cfg_path.read_text())["confirmed_captures"])
        enf.shutdown()


if __name__ == "__main__":
    unittest.main()
