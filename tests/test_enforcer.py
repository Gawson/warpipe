import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from helpers import FakePw, GraphBuilder, load_fixture
from scenarios import (base_system, monitor_mode, per_app, with_apps,
                       write_confirmed_config)

from warpipe.enforcer import Enforcer
from warpipe.graph import MANAGED_KEY, T_LINK, T_PORT, Graph
from warpipe.state import Config, SnapshotStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, b, confirm=True, **kw):
        """confirm=True: w konfiguracji są już potwierdzone strumienie „z monitora”
        (jak po jednorazowym wyborze w menu) — potrzebne w scenariuszach monitor_mode."""
        self.fake = FakePw(b)
        path = self.dir / "config.json"
        if confirm and not path.exists():
            write_confirmed_config(path)
        cfg = Config(path)
        store = SnapshotStore(self.dir / "run" / "snapshot.json")
        kw.setdefault("on_event", lambda k, m: self.events.append((k, m)))
        return Enforcer(self.fake, cfg, store, **kw)

    def inbound(self, b, node):
        return {l.out_node for l in Graph.from_dump(b.dump()).links.values() if l.in_node == node.id}

    def inbound_pairs(self, b, node):
        """{(port wyjściowy, port wejściowy, oznaczone?)} połączeń do węzła."""
        return {(l.out_port, l.in_port, l.managed) for l in Graph.from_dump(b.dump()).links.values()
                if l.in_node == node.id}

    def managed_count(self, b):
        return sum(1 for l in Graph.from_dump(b.dump()).links.values() if l.managed)


class TestApplyRestore(Base):
    def test_per_app_roundtrip(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe", "java"], persist=False)
        st = enf.refresh()
        self.assertIsNone(st.error)
        self.assertEqual(self.inbound(b, ns.cap_wf), {ns.warframe.id, ns.java.id})
        self.assertEqual(self.inbound(b, ns.cap_ff), set())
        # drugi przebieg nic nie zmienia
        n = len(self.fake.mutations)
        enf.refresh()
        self.assertEqual(len(self.fake.mutations), n)
        ok, msg = enf.restore()
        self.assertTrue(ok, msg)
        self.assertEqual(b.all_link_pairs(), before)
        self.assertTrue(enf.store.is_empty())

    def test_monitor_roundtrip_and_only_capture_links_touched(self):
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["Firefox"], persist=False)
        enf.refresh()
        self.assertEqual(self.inbound(b, ns.cap), {ns.firefox.id})
        for c in self.fake.mutations:
            port_in = c[2]
            self.assertIn(port_in, ns.cap.ports.values())
        enf.restore()
        self.assertEqual(b.all_link_pairs(), before)

    def test_snapshot_survives_new_process(self):
        """--apply (jeden proces) a potem --restore (drugi proces)."""
        b, ns = monitor_mode("alsa")
        before = b.all_link_pairs()
        e1 = self.make(b, enabled=True, selected=["Firefox"], persist=False)
        e1.refresh()
        self.assertTrue((self.dir / "run" / "snapshot.json").exists())
        e2 = self.make(b, enabled=False, persist=False)
        ok, _ = e2.restore()
        self.assertTrue(ok)
        self.assertEqual(b.all_link_pairs(), before)
        self.assertFalse((self.dir / "run" / "snapshot.json").exists())

    def test_disabled_does_nothing(self):
        b, ns = per_app()
        enf = self.make(b, enabled=False, selected=["java"], persist=False)
        st = enf.refresh()
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(st.capture_status, "found")
        self.assertEqual(st.routing_text(), "Do Discorda: cały dźwięk systemu")


class TestDynamics(Base):
    def test_capture_appears_later_auto_apply(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe"], persist=False)
        st = enf.refresh()
        self.assertEqual(st.capture_status, "none")
        self.assertEqual(self.fake.mutations, [])
        # Go Live z dźwiękiem: Discord tworzy discord_capture dla Firefoksa i Warframe
        cap_ff = b.capture(target=ns.firefox.serial)
        cap_wf = b.capture(target=ns.warframe.serial)
        b.connect(ns.firefox, cap_ff)
        b.connect(ns.warframe, cap_wf)
        st = enf.refresh()
        self.assertEqual(st.capture_status, "found")
        self.assertEqual(self.inbound(b, cap_ff), set())
        self.assertEqual(self.inbound(b, cap_wf), {ns.warframe.id})
        self.assertTrue(any(k == "capture_found" for k, _ in self.events))

    def test_app_restart_relinked(self):
        """(e) wybrana aplikacja restartuje się — nowe id, ten sam klucz."""
        b, ns = monitor_mode("ee_sink")
        enf = self.make(b, enabled=True, selected=["java"], persist=False)
        enf.refresh()
        self.assertEqual(self.inbound(b, ns.cap), {ns.java.id})
        b.remove_node(ns.java)
        enf.refresh()
        self.assertEqual(self.inbound(b, ns.cap), set())
        java2 = b.app(None, None, node_name="java", pid=False, client_app_name="java", api=None)
        b.connect(java2, ns.ee_sink, dst_prefix="playback")
        enf.refresh()
        self.assertEqual(self.inbound(b, ns.cap), {java2.id})

    def test_generic_capture_stays_recognised_without_links(self):
        """Węzeł rozpoznany tylko po połączeniach z monitora: po naszej zmianie i zamknięciu
        wybranej aplikacji nie ma żadnych połączeń — nadal ma być pilnowany (test na żywo W3)."""
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        cap = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, cap, src_prefix="monitor")
        enf = self.make(b, enabled=True, selected=["java"], persist=False)
        enf.refresh()
        self.assertEqual(self.inbound(b, cap), {ns.java.id})
        b.remove_node(ns.java)
        st = enf.refresh()
        self.assertEqual(self.inbound(b, cap), set())
        self.assertEqual(st.capture_status, "found")
        java2 = b.app(None, None, node_name="java", pid=False, client_app_name="java", api=None)
        enf.refresh()
        self.assertEqual(self.inbound(b, cap), {java2.id})
        # nowy proces (np. --restore) zna węzeł z migawki
        e2 = self.make(b, enabled=True, selected=["java"], persist=False)
        b.remove_node(java2)
        self.assertEqual(e2.refresh().capture_status, "found")

    def test_retry_soon_when_ports_missing(self):
        b, ns = monitor_mode("ee_sink")
        portless = b.node("Stream/Output/Audio", "late", props={"application.name": "Late"})
        enf = self.make(b, enabled=True, selected=["Late"], persist=False)
        self.assertTrue(enf.refresh().retry_soon)

    def test_discord_relinks_monitor_again(self):
        b, ns = monitor_mode("ee_sink")
        enf = self.make(b, enabled=True, selected=["Firefox"], persist=False)
        enf.refresh()
        b.connect(ns.ee_sink, ns.cap, src_prefix="monitor")  # Discord/WP podłącza znowu
        enf.refresh()
        self.assertEqual(self.inbound(b, ns.cap), {ns.firefox.id})
        # restore i tak odtworzy monitor dokładnie raz
        enf.restore()
        self.assertEqual(self.inbound(b, ns.cap), {ns.ee_sink.id})

    def test_loop_protection_pauses(self):
        b, ns = monitor_mode("ee_sink")
        clock = Clock()
        enf = self.make(b, enabled=True, selected=["Firefox"], persist=False, clock=clock,
                        loop_limit=3, loop_window=30.0, loop_pause=20.0)
        for _ in range(4):
            enf.refresh()
            b.connect(ns.ee_sink, ns.cap, src_prefix="monitor")  # ktoś ciągle przywraca
            clock.t += 1
        self.assertTrue(any(k == "loop" for k, _ in self.events))
        n = len(self.fake.mutations)
        st = enf.refresh()
        self.assertEqual(len(self.fake.mutations), n)  # wstrzymane
        self.assertTrue(st.paused)
        clock.t += 25
        enf.refresh()
        self.assertGreater(len(self.fake.mutations), n)


class TestErrors(Base):
    def test_dump_failure_is_harmless(self):
        b, ns = per_app()
        enf = self.make(b, enabled=True, selected=["java"], persist=False)
        self.fake.fail_dump = True
        st = enf.refresh()
        self.assertIsNotNone(st.error)
        self.assertIn("atrapa", st.error)
        self.assertEqual(self.fake.mutations, [])
        self.assertTrue(any(k == "error" for k, _ in self.events))

    def test_link_failure_skips_unlinks(self):
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["Firefox"], persist=False)
        self.fake.fail_link = True
        st = enf.refresh()
        self.assertIsNotNone(st.error)
        self.assertEqual(b.all_link_pairs(), before)
        self.assertFalse(any(c[0] == "unlink" for c in self.fake.calls))


class TestUiActions(Base):
    def test_check_app_enables_and_persists(self):
        b, ns = per_app()
        enf = self.make(b)
        self.assertFalse(enf.enabled)
        enf.set_app_selected("Warframe.x64.exe", True)
        self.assertTrue(enf.enabled)
        cfg = json.loads((self.dir / "config.json").read_text())
        self.assertEqual(cfg["selected_apps"], ["Warframe.x64.exe"])
        self.assertTrue(cfg["enforce"])
        self.assertEqual(self.inbound(b, ns.cap_ff), set())
        enf.set_enabled(False)
        cfg = json.loads((self.dir / "config.json").read_text())
        self.assertFalse(cfg["enforce"])
        self.assertEqual(self.inbound(b, ns.cap_ff), {ns.firefox.id})
        self.assertTrue(any(k == "restored" for k, _ in self.events))

    def test_override_cleared_when_node_gone(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        c1 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        c2 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, c1, src_prefix="monitor")
        b.connect(ns.alsa, c2, src_prefix="monitor")
        enf = self.make(b, enabled=True, selected=["Firefox"], persist=False)
        self.assertEqual(enf.refresh().capture_status, "ambiguous")
        self.assertEqual(self.fake.mutations, [])
        enf.set_capture_override([c2.serial])
        self.assertEqual(self.inbound(b, c2), {ns.firefox.id})
        self.assertEqual(self.inbound(b, c1), {ns.alsa.id})
        b.remove_node(c2)
        enf.refresh()
        self.assertIsNone(enf.override)

    def test_shutdown_restores(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["java"], persist=False)
        enf.refresh()
        ok, _ = enf.shutdown()
        self.assertTrue(ok)
        self.assertEqual(b.all_link_pairs(), before)


class TestPrivacy(Base):
    def test_media_name_never_in_files_or_logs(self):
        b, ns = per_app()  # Firefox ma media.name "SEKRETNY tytuł karty YouTube"
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = H(level=logging.DEBUG)
        lg = logging.getLogger("warpipe")
        old = lg.level
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
        try:
            enf = self.make(b)
            enf.set_app_selected("java", True)
            enf.set_app_selected("Firefox", True)
            enf.set_app_selected("Firefox", False)
            st = enf.status
            self.assertTrue(any("SEKRETNY" in h_ for a in st.apps for h_ in a.hints()))  # UI może pokazać
            enf.shutdown()
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        for rec in records:
            self.assertNotIn("SEKRETNY", rec)
        for k, m in self.events:
            self.assertNotIn("SEKRETNY", m)
        for p in self.dir.rglob("*"):
            if p.is_file():
                self.assertNotIn("SEKRETNY", p.read_text(encoding="utf-8"))
        # a migawka w trakcie (przed przywróceniem) też nie zawiera media.name
        enf = self.make(b, enabled=True, selected=["java"], persist=False)
        enf.refresh()
        snap = (self.dir / "run" / "snapshot.json").read_text(encoding="utf-8")
        self.assertNotIn("SEKRETNY", snap)
        self.assertNotIn("media.name", snap)
        enf.restore()


class TestReselectAndRestoreCycles(Base):
    """„Oznaczone” = „nie oryginał”: ponowne zaznaczenie aplikacji odtwarza jej
    oryginalne połączenie BEZ znacznika, więc kolejne przywrócenia go nie kasują."""

    def ff_pairs(self, ns, managed):
        return {(ns.firefox.p("output_FL"), ns.cap_ff.p("input_FL"), managed),
                (ns.firefox.p("output_FR"), ns.cap_ff.p("input_FR"), managed)}

    def test_untick_retick_whole_system_quit(self):
        b, ns = per_app()
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe"])
        enf.refresh()
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), set())
        enf.set_app_selected("Firefox", True)            # zaznacz z powrotem
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), self.ff_pairs(ns, False))
        enf.set_enabled(False)                           # „Cały system”
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), self.ff_pairs(ns, False))
        ok, msg = enf.shutdown()                         # „Zakończ”
        self.assertTrue(ok, msg)
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), self.ff_pairs(ns, False))
        self.assertEqual(b.all_link_pairs(), before)
        self.assertEqual(self.managed_count(b), 0)

    def test_variant_whole_system_enable_untick_whole_system(self):
        b, ns = per_app()
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe"])
        enf.refresh()
        enf.set_app_selected("Firefox", True)
        enf.set_enabled(False)
        enf.set_enabled(True)
        enf.set_app_selected("Firefox", False)
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), set())
        snap = json.loads((self.dir / "run" / "snapshot.json").read_text())
        ents = snap["captures"][str(ns.cap_ff.serial)]["links"]
        self.assertEqual(len(ents), 2)
        self.assertTrue(all(e["deleted"] for e in ents))
        enf.set_enabled(False)
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), self.ff_pairs(ns, False))

    def test_separate_processes_apply_A_AB_restore_A_restore(self):
        """CLI: --apply A; --apply A B; --restore; --apply A; --restore → B wraca."""
        b, ns = per_app()
        before = b.all_link_pairs()
        for sel in (["Warframe.x64.exe"], ["Warframe.x64.exe", "Firefox"]):
            self.make(b, enabled=True, selected=sel, persist=False).refresh()
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), self.ff_pairs(ns, False))
        self.assertTrue(self.make(b, enabled=False, persist=False).restore()[0])
        self.assertEqual(self.managed_count(b), 0)
        self.make(b, enabled=True, selected=["Warframe.x64.exe"], persist=False).refresh()
        self.assertEqual(self.inbound_pairs(b, ns.cap_ff), set())
        ok, msg = self.make(b, enabled=False, persist=False).restore()
        self.assertTrue(ok, msg)
        self.assertEqual(b.all_link_pairs(), before)
        self.assertEqual(self.managed_count(b), 0)
        # restore → restore: drugie nic nie zmienia
        ok, msg = self.make(b, enabled=False, persist=False).restore()
        self.assertTrue(ok)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(b.all_link_pairs(), before)
        self.assertEqual(msg, "przywrócono 0, usunięto 0")

    def test_multi_app_reselect_sequence(self):
        """Sekwencja z testu na żywo: apply browser; apply java; apply game+browser+java; restore×2."""
        b, ns = per_app()
        before = b.all_link_pairs()
        for sel in (["Firefox"], ["java"], ["Warframe.x64.exe", "Firefox", "java"]):
            self.make(b, enabled=True, selected=sel, persist=False).refresh()
        for _ in range(2):
            ok, _ = self.make(b, enabled=False, persist=False).restore()
            self.assertTrue(ok)
            self.assertEqual(b.all_link_pairs(), before)
            self.assertEqual(self.managed_count(b), 0)

    def test_monitor_mode_reselect_cycle(self):
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        enf = self.make(b, enabled=True, selected=["Firefox"])
        enf.refresh()
        enf.set_enabled(False)
        self.assertEqual(b.all_link_pairs(), before)
        enf.set_enabled(True)
        enf.set_app_selected("java", True)
        enf.set_enabled(False)
        enf.shutdown()
        self.assertEqual(b.all_link_pairs(), before)
        self.assertEqual(self.managed_count(b), 0)


class TestPipeWireRestart(Base):
    """Migawka w $XDG_RUNTIME_DIR przeżywa restart PipeWire, ale object.serial nie."""

    def crashed_session(self):
        b, ns = per_app()
        b.core(111)
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe"], persist=False)
        enf.refresh()   # potem kill -9: migawka zostaje
        snap = json.loads((self.dir / "run" / "snapshot.json").read_text())
        self.assertEqual(snap["pw_instance"], "111:3027")
        e = snap["captures"][str(ns.cap_ff.serial)]["links"][0]
        return snap, e

    def restarted_graph(self, e, cookie=222):
        b2 = GraphBuilder(load_fixture())
        b2.core(cookie, pid=4444)
        ports = [o for o in b2.objs if o.get("type") == T_PORT]
        mic = next(o for o in ports if o["info"]["props"].get("node.id") == 59 and o["info"]["direction"] == "output")
        hp = next(o for o in ports if o["info"]["props"].get("node.id") == 58
                  and o["info"]["props"].get("port.name") == "playback_FL")
        for o in b2.objs:
            pr = (o.get("info") or {}).get("props") or {}
            if pr.get("object.serial") in (e["out_port_serial"], e["in_port_serial"]):
                pr["object.serial"] = 900000 + o["id"]
        mic["info"]["props"]["object.serial"] = e["out_port_serial"]
        hp["info"]["props"]["object.serial"] = e["in_port_serial"]
        return b2

    def test_restore_after_restart_links_nothing(self):
        snap, e = self.crashed_session()
        b2 = self.restarted_graph(e)
        before = b2.all_link_pairs()
        enf = self.make(b2, enabled=False, persist=False)   # „warpipe --restore”
        ok, msg = enf.restore()
        self.assertTrue(ok, msg)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(b2.all_link_pairs(), before)
        self.assertFalse((self.dir / "run" / "snapshot.json").exists())

    def test_refresh_after_restart_drops_snapshot(self):
        snap, e = self.crashed_session()
        b2 = self.restarted_graph(e)
        enf = self.make(b2, enabled=True, selected=["java"], persist=False)
        enf.refresh()
        self.assertEqual(self.fake.mutations, [])
        self.assertFalse((self.dir / "run" / "snapshot.json").exists())

    def test_long_running_enforcer_forgets_serials(self):
        b, ns = per_app()
        b.core(1)
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe"], persist=False)
        enf.refresh()
        enf.override = [ns.cap_wf.serial]
        self.assertTrue(enf._confirmed)
        b.core(2)   # restart PipeWire (w atrapie te same numery — tym gorzej)
        enf.refresh()
        self.assertIsNone(enf.override)
        self.assertEqual(enf.store.instance, "2:3027")
        self.assertFalse((self.dir / "run" / "snapshot.json").exists())  # stara migawka odrzucona
        # przywrócenie niczego nie odtwarza z odrzuconej migawki
        n = len(self.fake.mutations)
        self.assertTrue(enf.restore()[0])
        self.assertEqual(len(self.fake.mutations), n)


class TestPortIdReuse(Base):
    """pw-link dostaje numery portów z wcześniejszego pw-dump; PipeWire używa
    zwolnionych numerów od razu. Połączenie, które trafiło nie tam, usuwamy."""

    def test_link_into_reused_port_id_is_removed(self):
        b, ns = per_app()
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe", "java"], persist=False)
        target_in = ns.cap_wf.p("input_FL")
        state = {"done": False}

        def swap(fake, out_port, in_port):
            if state["done"] or in_port != target_in:
                return
            state["done"] = True
            # Discord zamyka strumień, a numer portu dostaje wejście innego urządzenia
            b.remove_node(ns.cap_wf)
            other = b.node("Audio/Sink", "alsa_output.other-device")
            b.add_port(other, "playback_FL", "FL", "input", port_id=target_in)
            state["other"] = other

        self.fake.before_link = swap
        enf.refresh()
        self.assertTrue(state["done"])
        other = state["other"]
        self.assertEqual(self.inbound(b, other), set())
        self.assertIn(("unlink", ns.java.p("output_FL"), target_in), self.fake.calls)

    def test_foreign_link_on_reused_id_is_not_touched(self):
        """Gdy pw-link zwrócił „istnieje” (połączenie cudze), nic nie usuwamy."""
        b, ns = per_app()
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe", "java"], persist=False)
        target_in = ns.cap_wf.p("input_FL")
        state = {"done": False}

        def swap(fake, out_port, in_port):
            if state["done"] or in_port != target_in:
                return
            state["done"] = True
            b.remove_node(ns.cap_wf)
            other = b.node("Audio/Sink", "alsa_output.other-device")
            b.add_port(other, "playback_FL", "FL", "input", port_id=target_in)
            b.link(out_port, target_in)   # ktoś inny połączył to samo
            state["other"] = other

        self.fake.before_link = swap
        enf.refresh()
        self.assertEqual(self.inbound(b, state["other"]), {ns.java.id})
        self.assertNotIn(("unlink", ns.java.p("output_FL"), target_in), self.fake.calls)


class TestCaptureConfirmation(Base):
    """Strumień rozpoznany tylko po monitorze wyjścia wymaga jednorazowego potwierdzenia."""

    def test_not_applied_without_confirmation(self):
        b, ns = monitor_mode("ee_sink")
        enf = self.make(b, confirm=False, enabled=True, selected=["Firefox"])
        st = enf.refresh()
        self.assertEqual(st.capture_status, "ambiguous")
        self.assertEqual(self.fake.mutations, [])

    def test_manual_choice_is_remembered(self):
        b, ns = monitor_mode("ee_sink")
        enf = self.make(b, confirm=False, enabled=True, selected=["Firefox"])
        enf.set_capture_override([ns.cap.serial])
        self.assertEqual(self.inbound(b, ns.cap), {ns.firefox.id})
        cfg = json.loads((self.dir / "config.json").read_text())
        self.assertIn({"program": "Discord", "node": "Discord", "sink": "easyeffects_sink"},
                      cfg["confirmed_captures"])
        enf.shutdown()
        # nowy Go Live: nowy węzeł o tych samych cechach — wybrany sam
        b.remove_node(ns.cap)
        cap2 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream",
                         target=ns.ee_sink.name, capture_sink=True)
        b.connect(ns.ee_sink, cap2, src_prefix="monitor")
        enf2 = self.make(b, enabled=True, selected=["Firefox"], persist=False)
        self.assertEqual(enf2.refresh().capture_status, "found")
        self.assertEqual(self.inbound(b, cap2), {ns.firefox.id})


if __name__ == "__main__":
    unittest.main()
