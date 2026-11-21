"""Konfiguracja i migawka: odporność na uszkodzone pliki, wiązanie z uruchomieniem PipeWire."""

import json
import tempfile
import unittest
from pathlib import Path

from helpers import FakePw
from scenarios import per_app

from discord_dzwiek.enforcer import Enforcer
from discord_dzwiek.state import Config, SnapshotStore


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class TestConfigRobustness(Tmp):
    def test_non_utf8_config_uses_defaults(self):
        p = self.dir / "config.json"
        p.write_bytes('{"selected_apps": ["Gra Łódź.exe"], "enforce": true}'.encode("iso-8859-2"))
        c = Config(p)   # nie może rzucić UnicodeDecodeError
        self.assertIsNotNone(c.load_error)
        self.assertEqual(c.selected_apps, [])
        self.assertFalse(c.enforce)

    def test_not_an_object(self):
        p = self.dir / "config.json"
        p.write_text("[1, 2]")
        c = Config(p)
        self.assertIsNotNone(c.load_error)
        self.assertEqual(c.raw, {})

    def test_enforce_only_real_booleans(self):
        p = self.dir / "config.json"
        for raw, want in (('"false"', False), ('"true"', False), ("1", False), ("null", False),
                          ("true", True), ("false", False)):
            p.write_text('{"enforce": %s}' % raw)
            self.assertIs(Config(p).enforce, want, raw)

    def test_confirmed_captures_ignores_junk(self):
        p = self.dir / "config.json"
        p.write_text(json.dumps({"confirmed_captures": [
            {"program": "Discord", "node": "Discord", "sink": "s1"},
            {"program": "Discord", "node": "Discord"},
            "zły", 5,
            {"program": "Discord", "node": "Discord", "sink": "s1"},
        ]}))
        c = Config(p)
        self.assertEqual(c.confirmed_captures, [("Discord", "Discord", "s1")])
        self.assertEqual(c.detect_config().confirmed, (("Discord", "Discord", "s1"),))
        self.assertTrue(c.add_confirmed([("Discord", "Discord", "s2")]))
        self.assertFalse(c.add_confirmed([("Discord", "Discord", "s2")]))
        c.save()
        self.assertEqual(len(Config(p).confirmed_captures), 2)


class TestSnapshotRobustness(Tmp):
    def path(self):
        return self.dir / "run" / "snapshot.json"

    def write(self, data: bytes):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_bytes(data)

    def test_invalid_utf8(self):
        self.write(b'{"captures": {"\xff": {}}}')
        s = SnapshotStore(self.path())
        self.assertIsNotNone(s.load_error)
        self.assertEqual(s.captures, {})

    def test_wrong_shapes_are_dropped(self):
        self.write(json.dumps({"captures": {
            "123": "x",
            "abc": {"links": []},
            "5": {"node_name": 7, "links": "x"},
            "6": {"node_name": "discord_capture", "links": [1, "a", {"deleted": True}]},
        }, "pw_instance": 5}).encode())
        s = SnapshotStore(self.path())
        self.assertEqual(set(s.captures), {"5", "6"})
        self.assertEqual(s.captures["5"], {"node_name": None, "links": []})
        self.assertEqual(s.captures["6"]["links"], [{"deleted": True}])
        self.assertIsNone(s.instance)

    def test_malformed_snapshot_does_not_break_enforcer(self):
        b, ns = per_app()
        self.write(json.dumps({"version": 2, "pw_instance": None, "captures": {
            "123": "x",
            str(ns.cap_ff.serial): {"node_name": "discord_capture",
                                    "links": [{"out_port_serial": "x", "deleted": True}, 3]},
        }}).encode())
        fake = FakePw(b)
        enf = Enforcer(fake, Config(self.dir / "config.json"), SnapshotStore(self.path()),
                       enabled=True, selected=["Warframe.x64.exe"], persist=False)
        st = enf.refresh()            # nie TypeError
        self.assertIsNone(st.error)
        ok, msg = enf.restore()       # nie AttributeError
        self.assertTrue(ok, msg)
        self.assertEqual({l for l in b.links_into(ns.cap_ff)},
                         {(ns.firefox.p("output_FL"), ns.cap_ff.p("input_FL")),
                          (ns.firefox.p("output_FR"), ns.cap_ff.p("input_FR"))})

    def test_check_instance(self):
        s = SnapshotStore(self.path())
        self.assertFalse(s.check_instance("1:10"))        # pusta: tylko wiąże
        s.ensure_capture(5, "discord_capture", [{"x": 1}])
        self.assertFalse(s.check_instance("1:10"))        # to samo uruchomienie
        self.assertTrue(s.check_instance("2:20"))         # inne: odrzucona
        self.assertEqual(s.captures, {})
        self.assertEqual(s.instance, "2:20")
        s.ensure_capture(5, "discord_capture", [])
        s.clear()
        self.assertEqual(s.instance, "2:20")


if __name__ == "__main__":
    unittest.main()
