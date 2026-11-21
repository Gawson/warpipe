"""Kanał sterowania (control.sock): protokół, zasady przełączania, stan, gniazdo."""

import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakePw, GraphBuilder
from scenarios import base_system, per_app, with_apps

from warpipe import cli
from warpipe.control import (MAX_LINE, ControlError, ControlHandler,
                                    ControlServer, NotRunning, ProtocolError,
                                    parse_request, remove_stale_socket,
                                    request, status_to_dict)
from warpipe.enforcer import Enforcer
from warpipe.graph import Graph
from warpipe.pw import PwError
from warpipe.state import Config, SnapshotStore


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class TestParse(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_request(b'{"cmd":"status"}'), {"cmd": "status", "id": None})
        self.assertEqual(parse_request('{"cmd":"toggle","app":" Warframe.x64.exe ","id":7}'),
                         {"cmd": "toggle", "id": 7, "app": "Warframe.x64.exe"})
        self.assertEqual(parse_request(b'{"cmd":"set","app":"Firefox","on":"off","id":"a1"}'),
                         {"cmd": "set", "id": "a1", "app": "Firefox", "on": False})
        self.assertTrue(parse_request(b'{"cmd":"set","app":"Firefox","on":true}')["on"])
        self.assertEqual(parse_request(b'{"cmd":"system"}')["cmd"], "system")
        self.assertEqual(parse_request(b'{"cmd":"subscribe"}')["cmd"], "subscribe")
        self.assertEqual(parse_request('{"cmd":"toggle","app":"Gra Łódź.exe"}'.encode())["app"], "Gra Łódź.exe")

    def test_bad_ids_dropped(self):
        self.assertIsNone(parse_request(b'{"cmd":"status","id":true}')["id"])
        self.assertIsNone(parse_request(b'{"cmd":"status","id":{"x":1}}')["id"])
        self.assertIsNone(parse_request(json.dumps({"cmd": "status", "id": "x" * 100}).encode())["id"])

    def test_errors(self):
        bad = [
            b"nie json",
            b"[1,2]",
            b'{"app":"x"}',
            b'{"cmd":5}',
            b'\xff\xfe',
            b'{"cmd":"toggle"}',
            b'{"cmd":"toggle","app":""}',
            b'{"cmd":"toggle","app":"   "}',
            b'{"cmd":"toggle","app":7}',
            b'{"cmd":"toggle","app":"a\\nb"}',
            json.dumps({"cmd": "toggle", "app": "x" * 300}).encode(),
            b'{"cmd":"set","app":"Firefox"}',
            b'{"cmd":"set","app":"Firefox","on":1}',
            b'{"cmd":"rm -rf /"}',
            b"x" * (MAX_LINE + 1),
        ]
        for line in bad:
            with self.assertRaises(ProtocolError, msg=line[:60]):
                parse_request(line)

    def test_unknown_command_keeps_id_and_is_polish(self):
        with self.assertRaises(ProtocolError) as cm:
            parse_request(b'{"cmd":"exec","id":3}')
        self.assertEqual(cm.exception.rid, 3)
        self.assertIn("nieznane polecenie", str(cm.exception))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, b, **kw):
        self.fake = FakePw(b)
        self.cfg_path = self.dir / "config.json"
        kw.setdefault("on_event", lambda k, m: self.events.append((k, m)))
        return Enforcer(self.fake, Config(self.cfg_path), SnapshotStore(self.dir / "run" / "snapshot.json"), **kw)

    def inbound(self, b, node):
        return {l.out_node for l in Graph.from_dump(b.dump()).links.values() if l.in_node == node.id}

    def saved(self):
        return json.loads(self.cfg_path.read_text())


class TestStatus(Base):
    def test_status_content_per_app(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False, selected=["Spotify"])
        enf.refresh()
        d = status_to_dict(enf.status)
        self.assertEqual(d["version"], 1)
        self.assertFalse(d["enforce"])
        self.assertEqual(d["capture"], {"status": "active", "count": 2})
        by = {a["key"]: a for a in d["apps"]}
        self.assertEqual(set(by), {"Firefox", "java", "Warframe.x64.exe", "Spotify"})
        # stan faktyczny: WirePlumber połączył Warframe i Firefoksa z ich discord_capture
        self.assertTrue(by["Warframe.x64.exe"]["in_stream"])
        self.assertTrue(by["Firefox"]["in_stream"])
        self.assertFalse(by["java"]["in_stream"])
        self.assertTrue(by["java"]["present"])
        self.assertEqual(by["Spotify"], {"key": "Spotify", "display": "Spotify", "present": False,
                                         "selected": True, "in_stream": False})
        self.assertNotIn("error", d)
        text = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("SEKRETNY", text)  # media.name nigdy
        self.assertNotIn("WEBRTC", text)    # własne strumienie Discorda nie są aplikacjami

    def test_status_none_and_initial(self):
        b = GraphBuilder()
        with_apps(b, base_system(b))
        enf = self.make(b)
        d0 = status_to_dict(enf.status)  # przed pierwszym odświeżeniem
        self.assertEqual(d0["capture"], {"status": "none", "count": 0})
        enf.refresh()
        d = status_to_dict(enf.status)
        self.assertEqual(d["capture"]["status"], "none")
        self.assertFalse(any(a["in_stream"] for a in d["apps"]))

    def test_status_ambiguous(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        c1 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        c2 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, c1, src_prefix="monitor")
        b.connect(ns.alsa, c2, src_prefix="monitor")
        enf = self.make(b)
        enf.refresh()
        self.assertEqual(status_to_dict(enf.status)["capture"], {"status": "ambiguous", "count": 0})

    def test_pipewire_error_in_status(self):
        b, ns = per_app()
        enf = self.make(b)
        self.fake.fail_dump = True
        enf.refresh()
        d = status_to_dict(enf.status)
        self.assertEqual(d["capture"]["status"], "none")
        self.assertIn("pw-dump", d["error"])


class TestToggleRules(Base):
    def test_system_then_press_then_back_to_system(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=False)
        enf.refresh()
        h = ControlHandler(enf)
        # „Cały system” + Warframe → pilnowanie, tylko Warframe
        r = h.handle({"cmd": "toggle", "app": "Warframe.x64.exe", "id": 1})
        self.assertTrue(r["ok"], r)
        self.assertIn("tylko", r["message"])
        self.assertTrue(enf.enabled)
        self.assertEqual(enf.selected, ["Warframe.x64.exe"])
        self.assertEqual(self.inbound(b, ns.cap_ff), set())
        self.assertEqual(self.inbound(b, ns.cap_wf), {ns.warframe.id})
        by = {a["key"]: a for a in r["status"]["apps"]}
        self.assertTrue(by["Warframe.x64.exe"]["in_stream"])
        self.assertFalse(by["Firefox"]["in_stream"])
        self.assertEqual(self.saved()["selected_apps"], ["Warframe.x64.exe"])
        self.assertIs(self.saved()["enforce"], True)
        # + java (bez PID, bez własnego discord_capture → dołączona do nośnika)
        r = h.handle({"cmd": "toggle", "app": "java"})
        self.assertEqual(enf.selected, ["java", "Warframe.x64.exe"])
        self.assertTrue({a["key"]: a for a in r["status"]["apps"]}["java"]["in_stream"])
        # odznacz Warframe → zostaje java
        r = h.handle({"cmd": "toggle", "app": "Warframe.x64.exe"})
        self.assertTrue(enf.enabled)
        self.assertEqual(enf.selected, ["java"])
        self.assertIn("Odznaczono", r["message"])
        by = {a["key"]: a for a in r["status"]["apps"]}
        self.assertFalse(by["Warframe.x64.exe"]["in_stream"])
        self.assertTrue(by["java"]["in_stream"])
        # odznacz ostatnią → „Cały system” + przywrócenie
        r = h.handle({"cmd": "toggle", "app": "java"})
        self.assertTrue(r["ok"], r)
        self.assertFalse(enf.enabled)
        self.assertEqual(enf.selected, [])
        self.assertIn("cały dźwięk systemu", r["message"])
        self.assertEqual(b.all_link_pairs(), before)
        self.assertFalse(r["status"]["enforce"])
        self.assertEqual(self.saved()["selected_apps"], [])
        self.assertIs(self.saved()["enforce"], False)
        self.assertIn(("restored", "Do Discorda idzie cały dźwięk systemu."), self.events)

    def test_previous_selection_replaced_when_pressed_from_system(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False, selected=["Firefox", "java"])
        enf.refresh()
        ControlHandler(enf).handle({"cmd": "toggle", "app": "Warframe.x64.exe"})
        self.assertEqual(enf.selected, ["Warframe.x64.exe"])

    def test_case_insensitive_key(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False)
        enf.refresh()
        h = ControlHandler(enf)
        h.handle({"cmd": "toggle", "app": "warframe.X64.EXE"})
        self.assertEqual(enf.selected, ["Warframe.x64.exe"])
        h.handle({"cmd": "toggle", "app": "WARFRAME.x64.exe"})
        self.assertFalse(enf.enabled)

    def test_set_on_off(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=False)
        enf.refresh()
        h = ControlHandler(enf)
        r = h.handle({"cmd": "set", "app": "Firefox", "on": True})
        self.assertEqual((enf.enabled, enf.selected), (True, ["Firefox"]))
        n = len(self.fake.mutations)
        r = h.handle({"cmd": "set", "app": "Firefox", "on": True})
        self.assertIn("Bez zmian", r["message"])
        self.assertEqual(len(self.fake.mutations), n)
        r = h.handle({"cmd": "set", "app": "Firefox", "on": False})
        self.assertFalse(enf.enabled)
        self.assertEqual(b.all_link_pairs(), before)
        # „off” w trybie „Cały system” niczego nie zmienia
        r = h.handle({"cmd": "set", "app": "Firefox", "on": False})
        self.assertIn("Bez zmian", r["message"])
        self.assertFalse(enf.enabled)

    def test_system_command(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["java"])
        enf.refresh()
        self.assertNotEqual(b.all_link_pairs(), before)
        r = ControlHandler(enf).handle({"cmd": "system"})
        self.assertTrue(r["ok"])
        self.assertFalse(enf.enabled)
        self.assertEqual(enf.selected, ["java"])   # wybór zostaje zapamiętany
        self.assertEqual(b.all_link_pairs(), before)
        self.assertFalse(r["status"]["enforce"])

    def test_no_stream_selection_persisted_no_changes(self):
        b = GraphBuilder()
        with_apps(b, base_system(b))
        enf = self.make(b, enabled=False)
        enf.refresh()
        r = ControlHandler(enf).handle({"cmd": "toggle", "app": "Warframe.x64.exe"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"]["capture"]["status"], "none")
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.saved()["selected_apps"], ["Warframe.x64.exe"])
        self.assertTrue(any(k == "applied" for k, m in self.events))

    def test_pipewire_error_reply_not_ok(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False)
        enf.refresh()
        self.fake.fail_dump = True
        r = ControlHandler(enf).handle({"cmd": "toggle", "app": "Firefox"})
        self.assertFalse(r["ok"])
        self.assertIn("pw-dump", r["error"])

    def test_status_and_unknown(self):
        b, ns = per_app()
        enf = self.make(b)
        enf.refresh()
        h = ControlHandler(enf)
        self.assertTrue(h.handle({"cmd": "status"})["ok"])
        r = h.handle({"cmd": "nope"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.fake.mutations, [])


class SockBase(Base):
    def setUp(self):
        super().setUp()
        self.run_dir = self.dir / "run"
        self.path = self.run_dir / "control.sock"
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()
        super().tearDown()

    def serve(self, enf, **kw):
        srv = ControlServer(ControlHandler(enf), self.path, debounce=0.02, **kw)
        srv.start()
        enf.add_status_listener(srv.notify)
        self.servers.append(srv)
        return srv

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(self.path))
        return s, s.makefile("rb")

    def ask(self, f, s, obj):
        s.sendall((json.dumps(obj) + "\n").encode())
        return json.loads(f.readline())


class TestServer(SockBase):
    def test_request_reply_and_permissions(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False)
        enf.refresh()
        self.serve(enf)
        self.assertEqual(stat.S_IMODE(os.stat(self.run_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        r = request({"cmd": "status", "id": 5}, self.path)
        self.assertEqual(r["id"], 5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"]["capture"]["status"], "active")

    def test_error_replies_on_same_connection(self):
        b, ns = per_app()
        enf = self.make(b)
        enf.refresh()
        self.serve(enf)
        s, f = self.connect()
        try:
            s.sendall(b"to nie json\n")
            r = json.loads(f.readline())
            self.assertFalse(r["ok"])
            self.assertIn("JSON", r["error"])
            r = self.ask(f, s, {"cmd": "shell", "id": "x"})
            self.assertEqual((r["ok"], r["id"]), (False, "x"))
            self.assertIn("nieznane polecenie", r["error"])
            # połączenie dalej działa
            self.assertTrue(self.ask(f, s, {"cmd": "status"})["ok"])
            # za długa linia → błąd i rozłączenie
            s.sendall(b"a" * (MAX_LINE + 10))
            r = json.loads(f.readline())
            self.assertFalse(r["ok"])
            self.assertEqual(f.readline(), b"")
        finally:
            s.close()
        self.assertEqual(self.fake.mutations, [])

    def test_subscribe_gets_pushes_debounced(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False)
        enf.refresh()
        srv = self.serve(enf)
        s, f = self.connect()
        try:
            r = self.ask(f, s, {"cmd": "subscribe", "id": "sub"})
            self.assertEqual((r["ok"], r["id"]), (True, "sub"))
            self.assertFalse(r["status"]["enforce"])
            # inny klient przełącza aplikację
            r2 = request({"cmd": "toggle", "app": "Firefox"}, self.path)
            self.assertTrue(r2["ok"], r2)
            push = json.loads(f.readline())
            self.assertEqual(push["type"], "status")
            self.assertTrue(push["status"]["enforce"])
            by = {a["key"]: a for a in push["status"]["apps"]}
            self.assertTrue(by["Firefox"]["in_stream"] and by["Firefox"]["selected"])
            self.assertFalse(by["Warframe.x64.exe"]["in_stream"])
            # bez zmian → bez kolejnych wiadomości (ten sam stan nie jest wysyłany drugi raz)
            pushes = srv.pushes
            enf.refresh()
            enf.refresh()
            time.sleep(0.2)
            self.assertEqual(srv.pushes, pushes)
            # zmiana grafu (np. nowa aplikacja) → nowy stan
            b.app("Spotify", "spotify")
            enf.refresh()
            push = json.loads(f.readline())
            self.assertIn("Spotify", {a["key"] for a in push["status"]["apps"]})
            # subskrybent może dalej wysyłać polecenia
            s.sendall(b'{"cmd":"system","id":9}\n')
            msgs = [json.loads(f.readline()) for _ in range(2)]
            reply = next(m for m in msgs if m.get("type") == "reply")
            self.assertEqual(reply["id"], 9)
            self.assertFalse(enf.enabled)
        finally:
            s.close()

    def test_stop_removes_socket_and_disconnects(self):
        b, ns = per_app()
        enf = self.make(b)
        srv = self.serve(enf)
        s, f = self.connect()
        self.ask(f, s, {"cmd": "subscribe"})
        srv.stop()
        self.assertFalse(self.path.exists())
        self.assertEqual(f.readline(), b"")
        s.close()
        with self.assertRaises(NotRunning):
            request({"cmd": "status"}, self.path)

    def test_stale_socket_after_crash_is_replaced(self):
        self.run_dir.mkdir(parents=True)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(self.path))
        dead.close()                   # plik gniazda został, nikt nie nasłuchuje (jak po kill -9)
        with self.assertRaises(NotRunning):
            request({"cmd": "status"}, self.path)
        b, ns = per_app()
        enf = self.make(b)
        self.serve(enf)
        self.assertTrue(request({"cmd": "status"}, self.path)["ok"])

    def test_refuses_non_socket_and_live_listener(self):
        self.run_dir.mkdir(parents=True)
        self.path.write_text("nie gniazdo")
        with self.assertRaises(ControlError):
            remove_stale_socket(self.path)
        self.assertTrue(self.path.exists())
        self.path.unlink()
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(str(self.path))
        live.listen(1)
        try:
            with self.assertRaises(ControlError):
                remove_stale_socket(self.path)
            self.assertTrue(self.path.exists())
        finally:
            live.close()

    def test_max_clients(self):
        b, ns = per_app()
        enf = self.make(b)
        srv = self.serve(enf, max_clients=2)
        conns = [self.connect() for _ in range(2)]
        try:
            self.assertTrue(wait_for(lambda: srv.client_count == 2))
            s, f = self.connect()
            r = json.loads(f.readline())
            self.assertFalse(r["ok"])
            self.assertIn("za dużo", r["error"])
            s.close()
        finally:
            for s, _ in conns:
                s.close()

    def test_private_dir_tightened(self):
        self.run_dir.mkdir(parents=True, mode=0o755)
        os.chmod(self.run_dir, 0o755)
        b, ns = per_app()
        self.serve(self.make(b))
        self.assertEqual(stat.S_IMODE(os.stat(self.run_dir).st_mode), 0o700)


class TestCliRemote(SockBase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {
            "DISCORD_DZWIEK_CONFIG_DIR": str(self.dir / "cfg"),
            "DISCORD_DZWIEK_RUNTIME_DIR": str(self.run_dir),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "setup_logging", lambda *a, **k: None), \
                mock.patch.object(cli, "PwCli", lambda: self.fail("CLI zdalne nie może wołać PipeWire")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_not_running(self):
        for argv in (["--status"], ["--toggle", "Firefox"], ["--system"]):
            rc, out, err = self.run_cli(*argv)
            self.assertEqual(rc, cli.EXIT_NOT_RUNNING, argv)
            self.assertIn("nie jest uruchomiony", err)

    def test_status_toggle_system(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=False)
        enf.refresh()
        self.serve(enf)
        rc, out, err = self.run_cli("--status")
        self.assertEqual(rc, 0, err)
        self.assertIn("Stream z dźwiękiem: wykryty", out)
        self.assertIn("cały dźwięk systemu", out)
        self.assertIn("Warframe.x64.exe  — na streamie", out)
        rc, out, err = self.run_cli("--status", "--json")
        self.assertEqual(json.loads(out)["capture"]["status"], "active")
        rc, out, err = self.run_cli("--toggle", "Warframe.x64.exe")
        self.assertEqual(rc, 0, err)
        self.assertIn("Do Discorda tylko: Warframe.x64.exe", out)
        self.assertIn("[x] Warframe.x64.exe", out)
        self.assertEqual(self.inbound(b, ns.cap_ff), set())
        rc, out, err = self.run_cli("--system")
        self.assertEqual(rc, 0, err)
        self.assertIn("cały dźwięk systemu", out)
        self.assertEqual(b.all_link_pairs(), before)

    def test_usage(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--toggle", "x", "--system")
        with self.assertRaises(SystemExit):
            self.run_cli("--toggle", "x", "--json")
        with self.assertRaises(SystemExit):
            self.run_cli("--toggle", "  ")


class TestWatchModeControl(unittest.TestCase):
    """--watch bez --apply: tryb i wybór z konfiguracji (jak ikona) + kanał sterowania."""

    def test_watch_serves_and_cleans_up(self):
        from scenarios import per_app as _per_app
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"DISCORD_DZWIEK_CONFIG_DIR": str(d / "cfg"),
                                           "DISCORD_DZWIEK_RUNTIME_DIR": str(d / "run")})
        env.start()
        self.addCleanup(env.stop)
        b, ns = _per_app()
        before = b.all_link_pairs()
        fake = FakePw(b)
        stopped = threading.Event()
        holder = {}

        class FakeWatcher:
            def __init__(self, enf, pw, **kw):
                holder["enf"] = enf
                self.enf = enf

            def run(self):
                self.enf.refresh()
                stopped.wait(10)

            def stop(self):
                stopped.set()

        rc = {}

        def target():
            with mock.patch.object(cli, "PwCli", lambda: fake), \
                    mock.patch.object(cli, "Watcher", FakeWatcher), \
                    mock.patch.object(cli, "setup_logging", lambda *a, **k: None), \
                    mock.patch.object(cli.signal, "signal", lambda *a: None):
                rc["rc"] = cli.main(["--watch"])

        t = threading.Thread(target=target, daemon=True)
        t.start()
        sock = d / "run" / "control.sock"
        self.assertTrue(wait_for(lambda: sock.exists() and "enf" in holder))
        enf = holder["enf"]
        self.assertFalse(enf.enabled)   # konfiguracja: enforce=false → „Cały system”
        r = request({"cmd": "toggle", "app": "Firefox"}, sock)
        self.assertTrue(r["ok"], r)
        cfg = json.loads((d / "cfg" / "config.json").read_text())
        self.assertEqual((cfg["selected_apps"], cfg["enforce"]), (["Firefox"], True))
        stopped.set()
        t.join(5)
        self.assertEqual(rc.get("rc"), 0)
        self.assertFalse(sock.exists())
        self.assertEqual(b.all_link_pairs(), before)  # wyjście przywraca


if __name__ == "__main__":
    unittest.main()
