"""Ikona w zasobniku bez wyświetlania (QT_QPA_PLATFORM=offscreen)."""

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

from helpers import FakePw, GraphBuilder
from scenarios import base_system, monitor_mode, per_app, with_apps

from discord_dzwiek.enforcer import Enforcer
from discord_dzwiek.state import Config, SnapshotStore


@unittest.skipUnless(HAVE_QT, "brak PySide6")
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
        from discord_dzwiek.tray import TrayController
        self.enf.refresh()
        ctl = TrayController(self.enf)
        texts = self.texts(ctl.menu)
        self.assertEqual(texts[0], "Stream z dźwiękiem: wykryty")
        self.assertIn("Tylko zaznaczone do Discorda", texts)
        self.assertIn("Cały system (przywróć)", texts)
        self.assertIn("Zakończ", texts)
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
        # „Cały system”
        ctl.act_restore.trigger()
        self.app.processEvents()
        self.assertFalse(self.enf.enabled)
        self.assertFalse(ctl.act_enforce.isChecked())

    def test_about_to_show_rebuilds(self):
        from discord_dzwiek.tray import TrayController
        ctl = TrayController(self.enf)
        newapp = self.b.app("Spotify", "spotify")
        ctl.menu.aboutToShow.emit()
        self.app.processEvents()
        self.assertIn("Spotify", ctl.app_actions)

    def test_ambiguous_submenu(self):
        from discord_dzwiek.tray import TrayController
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
        self.assertEqual(sub_texts[0], "Automatycznie")
        self.assertEqual(len(sub_texts), 3)

    def test_monitor_only_capture_confirmed_from_menu(self):
        """Jedyny strumień „z monitora”: menu prosi o potwierdzenie; wybór zostaje zapamiętany."""
        import json
        from discord_dzwiek.tray import TrayController
        b, ns = monitor_mode("ee_sink")
        cfg_path = Path(self.tmp.name) / "c3.json"
        enf = Enforcer(FakePw(b), Config(cfg_path), SnapshotStore(Path(self.tmp.name) / "s3.json"),
                       enabled=True, selected=["Firefox"])
        st = enf.refresh()
        self.assertEqual(st.capture_status, "ambiguous")
        ctl = TrayController(enf)
        sub = next(a.menu() for a in ctl.menu.actions() if a.menu() is not None)
        pick = next(a for a in sub.actions() if a.text().startswith(f"#{ns.cap.id} "))
        self.assertIn("mikrofon", pick.text())
        pick.trigger()
        self.app.processEvents()
        self.assertEqual(enf.status.capture_status, "found")
        self.assertTrue(json.loads(cfg_path.read_text())["confirmed_captures"])
        enf.shutdown()


if __name__ == "__main__":
    unittest.main()
