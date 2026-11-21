import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakePw, GraphBuilder, load_fixture
from scenarios import (base_system, monitor_mode, per_app, with_apps,
                       write_confirmed_config)

from warpipe import cli
from warpipe.graph import Graph
from warpipe.scrub import scrub_dump


class TestScrub(unittest.TestCase):
    def test_media_name_and_identity_scrubbed(self):
        b, ns = per_app()
        b.core(123456)
        # klienci też mają core.name = "pipewire-<użytkownik>-<pid>"
        b.client("Firefox", "firefox", **{"core.name": "pipewire-tester-999"})
        objs = b.dump()
        # Tag z płaską listą klucz/wartość, jak w params portów
        objs.append({"id": 77777, "type": "PipeWire:Interface:Port", "info": {
            "direction": "output", "props": {"node.id": ns.firefox.id, "port.name": "x"},
            "params": {"Tag": [{"direction": "Output", "info": [2, "media.name", "SEKRETNY", "media.class", "Stream/Output/Audio"]}]}}})
        out = scrub_dump(objs)
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("SEKRETNY", text)
        self.assertNotIn("tester", text)   # nazwa użytkownika (Core user-name, core.name)
        self.assertNotIn("testhost", text)  # nazwa hosta (Core host-name)
        self.assertIn("Stream/Output/Audio", text)
        ff = next(o for o in out if o["id"] == ns.firefox.id)
        self.assertEqual(ff["info"]["props"]["media.name"], "<media>")
        self.assertEqual(ff["info"]["props"]["application.process.id"], "<scrubbed>")  # klucz zostaje
        # model działa dalej na zanonimizowanym zrzucie
        g = Graph.from_dump(out)
        self.assertTrue(g.nodes[ns.firefox.id].has_pid)
        self.assertEqual(g.nodes[ns.firefox.id].app_name, "Firefox")

    def test_original_not_modified(self):
        objs = load_fixture()
        before = json.dumps(objs, sort_keys=True)
        scrub_dump(objs)
        self.assertEqual(json.dumps(objs, sort_keys=True), before)


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "DISCORD_DZWIEK_CONFIG_DIR": str(d / "cfg"),
            "DISCORD_DZWIEK_RUNTIME_DIR": str(d / "run"),
        })
        self.env.start()
        self.dir = d
        # potwierdzone strumienie „z monitora” (jak po jednorazowym wyborze w menu)
        write_confirmed_config(d / "cfg" / "config.json")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, b, *argv):
        self.fake = FakePw(b)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "PwCli", lambda: self.fake), \
                mock.patch.object(cli, "setup_logging", lambda *a, **k: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class TestCli(CliBase):
    def test_list_json_no_media_name(self):
        b, ns = per_app()
        rc, out, err = self.run_cli(b, "--list", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["status"], "found")
        self.assertEqual(set(data["group"]), {ns.cap_wf.id, ns.cap_ff.id})
        self.assertNotIn("SEKRETNY", out)
        keys = {a["key"] for a in data["apps"]}
        self.assertEqual(keys, {"Firefox", "java", "Warframe.x64.exe"})
        self.assertEqual(self.fake.mutations, [])

    def test_list_human_sample(self):
        b = GraphBuilder(load_fixture())
        rc, out, err = self.run_cli(b, "--list")
        self.assertEqual(rc, 0)
        self.assertIn("nie wykryty", out)
        self.assertIn("mikrofon", out)
        self.assertEqual(self.fake.mutations, [])

    def test_apply_dry_run_changes_nothing(self):
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        rc, out, err = self.run_cli(b, "--apply", "firefox", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("połącz", out)
        self.assertIn("rozłącz", out)
        self.assertEqual(b.all_link_pairs(), before)
        self.assertEqual(self.fake.mutations, [])

    def test_apply_then_restore(self):
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        rc, out, err = self.run_cli(b, "--apply", "Firefox")
        self.assertEqual(rc, 0, err)
        inbound = {l.out_node for l in Graph.from_dump(b.dump()).links.values() if l.in_node == ns.cap.id}
        self.assertEqual(inbound, {ns.firefox.id})
        rc, out, err = self.run_cli(b, "--restore")
        self.assertEqual(rc, 0, err)
        self.assertEqual(b.all_link_pairs(), before)

    def test_apply_ambiguous_refuses(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        c1 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        c2 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, c1, src_prefix="monitor")
        b.connect(ns.alsa, c2, src_prefix="monitor")
        rc, out, err = self.run_cli(b, "--apply", "Firefox")
        self.assertEqual(rc, cli.EXIT_NO_CAPTURE)
        self.assertIn("--capture", err)
        self.assertEqual(self.fake.mutations, [])
        rc, out, err = self.run_cli(b, "--apply", "Firefox", "--capture", str(c1.id))
        self.assertEqual(rc, 0, err)
        inbound = {l.out_node for l in Graph.from_dump(b.dump()).links.values() if l.in_node == c1.id}
        self.assertEqual(inbound, {ns.firefox.id})

    def test_apply_no_capture_on_sample(self):
        b = GraphBuilder(load_fixture())
        rc, out, err = self.run_cli(b, "--apply", "java")
        self.assertEqual(rc, cli.EXIT_NO_CAPTURE)
        self.assertEqual(self.fake.mutations, [])

    def test_apply_refuses_mic_capture(self):
        b, ns = per_app()
        rc, out, err = self.run_cli(b, "--apply", "Firefox", "--capture", str(ns.rec.id))
        self.assertEqual(rc, cli.EXIT_NO_CAPTURE)
        self.assertIn("mikrofon", err)
        self.assertEqual(self.fake.mutations, [])

    def test_dump_scrubbed(self):
        b, ns = per_app()
        path = self.dir / "dump.json"
        rc, out, err = self.run_cli(b, "--dump", str(path))
        self.assertEqual(rc, 0, err)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("SEKRETNY", text)
        self.assertEqual(len(json.loads(text)), len(b.dump()))

    def test_lock_blocks_second_instance(self):
        from warpipe.state import InstanceLock
        lock = InstanceLock()
        self.assertTrue(lock.acquire())
        try:
            b, ns = monitor_mode("ee_sink")
            rc, out, err = self.run_cli(b, "--apply", "Firefox")
            self.assertEqual(rc, cli.EXIT_LOCKED)
            self.assertEqual(self.fake.mutations, [])
        finally:
            lock.release()

    def test_apply_typo_changes_nothing(self):
        """Literówka w nazwie nie może wyciszyć streamu: kod 2, bez zmian, lista nazw."""
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        rc, out, err = self.run_cli(b, "--apply", "firefx")
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(b.all_link_pairs(), before)
        self.assertIn("firefx", err)
        self.assertIn("Firefox", err)   # podpowiedź: dostępne nazwy
        rc, out, err = self.run_cli(b, "--apply", "firefx", "--dry-run")
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_apply_partial_match_warns_clearly(self):
        b, ns = monitor_mode("ee_sink")
        rc, out, err = self.run_cli(b, "--apply", "Firefox", "warfrme")
        self.assertEqual(rc, 0, err)
        self.assertIn("warfrme", err)
        self.assertIn("NIE pójdzie do Discorda", err)
        inbound = {l.out_node for l in Graph.from_dump(b.dump()).links.values() if l.in_node == ns.cap.id}
        self.assertEqual(inbound, {ns.firefox.id})

    def test_monitor_capture_needs_confirmation_then_remembered(self):
        b, ns = monitor_mode("ee_sink")
        (self.dir / "cfg" / "config.json").unlink()   # bez potwierdzeń
        rc, out, err = self.run_cli(b, "--apply", "Firefox")
        self.assertEqual(rc, cli.EXIT_NO_CAPTURE)
        self.assertIn("mikrofon", err)
        self.assertEqual(self.fake.mutations, [])
        rc, out, err = self.run_cli(b, "--list")
        self.assertIn(f"--capture {ns.cap.id}", out)
        rc, out, err = self.run_cli(b, "--apply", "Firefox", "--capture", str(ns.cap.id))
        self.assertEqual(rc, 0, err)
        cfg = json.loads((self.dir / "cfg" / "config.json").read_text())
        self.assertEqual(cfg["confirmed_captures"],
                         [{"program": "Discord", "node": "Discord", "sink": "easyeffects_sink"}])
        self.assertEqual(self.run_cli(b, "--restore")[0], 0)
        # potem już bez --capture
        rc, out, err = self.run_cli(b, "--apply", "Firefox")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.run_cli(b, "--restore")[0], 0)

    def test_corrupt_config_and_snapshot_do_not_crash(self):
        b, ns = monitor_mode("ee_sink")
        (self.dir / "cfg" / "config.json").write_bytes('{"selected_apps": ["Gra Łódź.exe"]}'.encode("iso-8859-2"))
        rc, out, err = self.run_cli(b, "--list")
        self.assertEqual(rc, 0, err)
        self.assertIn("Uwaga", err)
        (self.dir / "run").mkdir(parents=True, exist_ok=True)
        for bad in (b'{"captures": {"\xff": {}}}',
                    b'{"captures": {"123": "x", "5": {"links": [1, "a"]}, "7": {"links": "x"}}}',
                    b'[1, 2]'):
            (self.dir / "run" / "snapshot.json").write_bytes(bad)
            rc, out, err = self.run_cli(b, "--restore")
            self.assertEqual(rc, 0, (bad, err))
            self.assertEqual(self.fake.mutations, [])

    def test_usage_errors(self):
        b, ns = per_app()
        with self.assertRaises(SystemExit):
            self.run_cli(b, "--list", "--restore")
        with self.assertRaises(SystemExit):
            self.run_cli(b, "--json")


if __name__ == "__main__":
    unittest.main()
