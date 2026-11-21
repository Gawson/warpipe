"""Tray icon without showing anything (QT_QPA_PLATFORM=offscreen)."""

import os
import tempfile
import unittest
from pathlib import Path

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
        from PySide6.QtCore import QSize

        from warpipe.tray import load_tray_icon, tray_icon_files
        for variant in ("light", "dark"):
            for state in ("", "active", "off"):
                files = tray_icon_files(variant, state)
                self.assertIsNotNone(files, (variant, state))
                self.assertIsNotNone(files[1], (variant, state))   # 16 px version
                icon = load_tray_icon(variant, state)
                self.assertFalse(icon.isNull())
                sizes = icon.availableSizes()
                self.assertIn(QSize(16, 16), sizes)
                self.assertIn(QSize(22, 22), sizes)

    def test_fallback_without_files(self):
        from warpipe.tray import load_tray_icon
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(load_tray_icon("light", "active", Path(d)).isNull())

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
