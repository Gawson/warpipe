import unittest

from helpers import FakePw, GraphBuilder
from scenarios import CFG_MON, base_system, monitor_mode, per_app, with_apps

from discord_dzwiek.graph import Graph, Port
from discord_dzwiek.planner import (DetectConfig, channel_pairs, detect,
                                    link_entry, plan_apply, plan_restore,
                                    snapshot_originals)

CFG = DetectConfig()


def g(b):
    return Graph.from_dump(b.dump())


def P(id, ch, name=None):
    return Port(id=id, serial=id, node_id=0, direction="x", name=name or f"p_{ch}", channel=ch,
                monitor=False, port_index=id)


def run_plan(b, plan, fake=None):
    """Wykonuje plan na builderze w kolejności plan.ops (jak Enforcer)."""
    fake = fake or FakePw(b)
    for op in plan.ops:
        if op.kind == "link":
            fake.link(op.out_port, op.in_port, managed=op.managed)
        else:
            fake.unlink(op.out_port, op.in_port)
    return fake


class TestChannels(unittest.TestCase):
    """(f) kombinacje mono/stereo."""

    def test_stereo_to_stereo(self):
        self.assertEqual(channel_pairs([P(1, "FL"), P(2, "FR")], [P(10, "FL"), P(11, "FR")]),
                         [(1, 10), (2, 11)])

    def test_stereo_swapped_order_matched_by_channel(self):
        self.assertEqual(sorted(channel_pairs([P(1, "FR"), P(2, "FL")], [P(10, "FL"), P(11, "FR")])),
                         [(1, 11), (2, 10)])

    def test_mono_app_to_stereo_capture(self):
        self.assertEqual(sorted(channel_pairs([P(1, "MONO")], [P(10, "FL"), P(11, "FR")])),
                         [(1, 10), (1, 11)])

    def test_stereo_app_to_mono_capture(self):
        self.assertEqual(sorted(channel_pairs([P(1, "FL"), P(2, "FR")], [P(10, "MONO")])),
                         [(1, 10), (2, 10)])

    def test_mono_to_mono(self):
        self.assertEqual(channel_pairs([P(1, "MONO")], [P(10, "MONO")]), [(1, 10)])

    def test_surround_downmix(self):
        outs = [P(1, "FL"), P(2, "FR"), P(3, "FC"), P(4, "LFE"), P(5, "RL"), P(6, "RR")]
        pairs = set(channel_pairs(outs, [P(10, "FL"), P(11, "FR")]))
        self.assertEqual(pairs, {(1, 10), (2, 11), (3, 10), (3, 11), (4, 10), (4, 11), (5, 10), (6, 11)})

    def test_unknown_channels_positional(self):
        outs = [P(1, None, "output_1"), P(2, None, "output_2")]
        ins = [P(10, None, "input_1"), P(11, None, "input_2")]
        self.assertEqual(channel_pairs(outs, ins), [(1, 10), (2, 11)])

    def test_empty(self):
        self.assertEqual(channel_pairs([], [P(10, "FL")]), [])
        self.assertEqual(channel_pairs([P(1, "FL")], []), [])


class TestPlanPerApp(unittest.TestCase):
    def test_select_game_only(self):
        b, ns = per_app()
        gr = g(b)
        det = detect(gr, CFG)
        plan = plan_apply(gr, det.group_nodes, ["Warframe.x64.exe"], CFG)
        self.assertEqual(plan.links, [])
        self.assertEqual({(o.out_port, o.in_port) for o in plan.unlinks},
                         {(ns.firefox.p("output_FL"), ns.cap_ff.p("input_FL")),
                          (ns.firefox.p("output_FR"), ns.cap_ff.p("input_FR"))})

    def test_pidless_app_goes_to_carrier(self):
        """Minecraft (java, bez PID) nie ma własnego discord_capture — idzie do „nośnika”."""
        b, ns = per_app()
        gr = g(b)
        det = detect(gr, CFG)
        plan = plan_apply(gr, det.group_nodes, ["Warframe.x64.exe", "java"], CFG)
        # nośnik: przechwytywanie wybranej aplikacji (Warframe), nie Firefoksa
        self.assertEqual({(o.out_port, o.in_port) for o in plan.links},
                         {(ns.java.p("output_FL"), ns.cap_wf.p("input_FL")),
                          (ns.java.p("output_FR"), ns.cap_wf.p("input_FR"))})
        self.assertTrue(all(o.managed for o in plan.links))

    def test_only_pidless_app_uses_unselected_capture_as_carrier(self):
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["java"], CFG)
        dests = {o.in_node for o in plan.links}
        self.assertEqual(len(dests), 1)
        run_plan(b, plan)
        gr2 = g(b)
        # każda wybrana aplikacja dokładnie raz, nic innego
        inbound = [l for l in gr2.links.values() if l.in_node in (ns.cap_wf.id, ns.cap_ff.id)]
        self.assertEqual({l.out_node for l in inbound}, {ns.java.id})
        self.assertEqual(len(inbound), 2)

    def test_idempotent_after_apply(self):
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["java", "Firefox"], CFG)
        run_plan(b, plan)
        gr2 = g(b)
        plan2 = plan_apply(gr2, detect(gr2, CFG).group_nodes, ["java", "Firefox"], CFG)
        self.assertEqual(plan2.ops, [])

    def test_sticky_carrier(self):
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["java"], CFG)
        run_plan(b, plan)
        carrier = {o.in_node for o in plan.links}.pop()
        # nowa aplikacja bez PID — trafia do tego samego nośnika
        extra = b.app(None, None, node_name="mpv", pid=False, client_app_name="mpv", api=None)
        gr2 = g(b)
        plan2 = plan_apply(gr2, detect(gr2, CFG).group_nodes, ["java", "mpv"], CFG)
        self.assertEqual({o.in_node for o in plan2.links}, {carrier})
        self.assertEqual({o.out_node for o in plan2.links}, {extra.id})

    def test_empty_selection_silences(self):
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, [], CFG)
        self.assertEqual(plan.links, [])
        self.assertEqual(len(plan.unlinks), 4)

    def test_never_touches_links_outside_group(self):
        b, ns = per_app()
        gr = g(b)
        group = detect(gr, CFG).group_nodes
        ids = {n.id for n in group}
        for sel in ([], ["java"], ["Firefox", "Warframe.x64.exe", "java"]):
            plan = plan_apply(gr, group, sel, CFG)
            self.assertTrue(all(o.in_node in ids for o in plan.ops), sel)

    def test_app_restart_new_ids(self):
        """(e) wybrana aplikacja restartuje się: nowy id i serial, ten sam klucz."""
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe", "java"], CFG)
        run_plan(b, plan)
        b.remove_node(ns.java)
        b.remove_node(ns.cap_wf)
        b.remove_node(ns.warframe)
        wf2 = b.app("Warframe.x64.exe", "wine64-preloader", node_name="Warframe.x64.exe")
        java2 = b.app(None, None, node_name="java", pid=False, client_app_name="java", api=None)
        cap2 = b.capture(target=wf2.serial)
        b.connect(wf2, cap2)
        gr2 = g(b)
        det2 = detect(gr2, CFG)
        self.assertEqual({c.id for c in det2.group}, {cap2.id, ns.cap_ff.id})
        plan2 = plan_apply(gr2, det2.group_nodes, ["Warframe.x64.exe", "java"], CFG)
        self.assertEqual({(o.out_port, o.in_port) for o in plan2.links},
                         {(java2.p("output_FL"), cap2.p("input_FL")), (java2.p("output_FR"), cap2.p("input_FR"))})
        self.assertEqual(plan2.unlinks, [])  # Firefox był już odłączony


class TestPlanMonitor(unittest.TestCase):
    def test_replace_monitor_with_selected_apps(self):
        b, ns = monitor_mode("ee_sink")
        gr = g(b)
        det = detect(gr, CFG_MON)
        plan = plan_apply(gr, det.group_nodes, ["Firefox", "java"], CFG)
        self.assertEqual({(o.out_port, o.in_port) for o in plan.links}, {
            (ns.firefox.p("output_FL"), ns.cap.p("input_FL")), (ns.firefox.p("output_FR"), ns.cap.p("input_FR")),
            (ns.java.p("output_FL"), ns.cap.p("input_FL")), (ns.java.p("output_FR"), ns.cap.p("input_FR"))})
        self.assertEqual({(o.out_port, o.in_port) for o in plan.unlinks}, {
            (ns.ee_sink.p("monitor_FL"), ns.cap.p("input_FL")), (ns.ee_sink.p("monitor_FR"), ns.cap.p("input_FR"))})
        self.assertTrue(all(not o.managed for o in plan.unlinks))

    def test_mono_capture(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        cap = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream", channels=("MONO",))
        b.link(ns.alsa.p("monitor_FL"), cap.p("input_MONO"))
        gr = g(b)
        det = detect(gr, CFG_MON)
        plan = plan_apply(gr, det.group_nodes, ["Firefox"], CFG)
        self.assertEqual({(o.out_port, o.in_port) for o in plan.links},
                         {(ns.firefox.p("output_FL"), cap.p("input_MONO")),
                          (ns.firefox.p("output_FR"), cap.p("input_MONO"))})

    def test_mono_app(self):
        b, ns = monitor_mode("alsa")
        mono = b.app("Mono Game", "monogame", channels=("MONO",))
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG_MON).group_nodes, ["mono game"], CFG)  # wielkość liter bez znaczenia
        self.assertEqual({(o.out_port, o.in_port) for o in plan.links},
                         {(mono.p("output_MONO"), ns.cap.p("input_FL")),
                          (mono.p("output_MONO"), ns.cap.p("input_FR"))})


class TestRestore(unittest.TestCase):
    def _snapshot_from(self, gr, plan):
        caps = {}
        for op in plan.unlinks:
            if op.managed:
                continue
            cap = gr.nodes[op.in_node]
            e = link_entry(gr, gr.links[op.link_id], deleted=True)
            caps.setdefault(str(cap.serial), {"node_name": cap.name, "links": []})["links"].append(e)
        return {"version": 2, "pw_instance": gr.instance_id, "captures": caps}

    def test_roundtrip_monitor(self):
        b, ns = monitor_mode("ee_sink")
        before = b.all_link_pairs()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG_MON).group_nodes, ["Firefox"], CFG)
        snap = self._snapshot_from(gr, plan)
        run_plan(b, plan)
        self.assertNotEqual(b.all_link_pairs(), before)
        rplan = plan_restore(g(b), snap)
        run_plan(b, rplan)
        self.assertEqual(b.all_link_pairs(), before)
        # po przywróceniu nie zostają nasze połączenia
        self.assertFalse(any(l.managed for l in g(b).links.values()))

    def test_restore_skips_vanished_ports(self):
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = self._snapshot_from(gr, plan)
        run_plan(b, plan)
        b.remove_node(ns.firefox)  # Firefox zamknięty — nie ma czego odtwarzać
        rplan = plan_restore(g(b), snap)
        self.assertEqual(rplan.ops, [])

    def test_reselect_recreates_original_without_tag(self):
        """Ponowne zaznaczenie aplikacji odtwarza jej oryginalne połączenie BEZ znacznika."""
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = self._snapshot_from(gr, plan)
        run_plan(b, plan)
        gr2 = g(b)
        orig = snapshot_originals(gr2, snap)
        self.assertEqual(len(orig), 2)
        plan2 = plan_apply(gr2, detect(gr2, CFG).group_nodes, ["Warframe.x64.exe", "Firefox"], CFG, orig)
        self.assertEqual(len(plan2.links), 2)
        self.assertTrue(all(o.original and not o.managed for o in plan2.links))
        run_plan(b, plan2)
        self.assertFalse(any(l.managed for l in g(b).links.values()))

    def test_restore_replaces_tagged_original_with_untagged(self):
        """Oznaczona kopia oryginału (np. ze starszej wersji) zostaje zamieniona na
        nieoznaczoną — inaczej następne przywrócenie by ją skasowało."""
        b, ns = per_app()
        before = b.all_link_pairs()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = self._snapshot_from(gr, plan)
        run_plan(b, plan)
        gr2 = g(b)
        plan2 = plan_apply(gr2, detect(gr2, CFG).group_nodes, ["Warframe.x64.exe", "Firefox"], CFG)  # bez originals
        self.assertTrue(all(o.managed for o in plan2.links))
        run_plan(b, plan2)
        rplan = plan_restore(g(b), snap)
        kinds = [(o.kind, o.managed) for o in rplan.ops]
        self.assertEqual(kinds, [("unlink", True), ("link", False), ("unlink", True), ("link", False)])
        run_plan(b, rplan)
        self.assertEqual(b.all_link_pairs(), before)
        self.assertFalse(any(l.managed for l in g(b).links.values()))
        # drugie przywrócenie z pustą migawką niczego nie rusza
        self.assertEqual(plan_restore(g(b), {"captures": {}}).ops, [])


class TestRestoreStaleSnapshot(unittest.TestCase):
    """Migawka przeżywa restart PipeWire (leży w $XDG_RUNTIME_DIR), a object.serial
    to licznik demona — po restarcie te same numery mają zupełnie inne porty."""

    def session1(self, cookie):
        b, ns = per_app()
        b.core(cookie)
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = TestRestore._snapshot_from(None, gr, plan)
        self.assertEqual(snap["pw_instance"], f"{cookie}:3027")
        e = snap["captures"][str(ns.cap_ff.serial)]["links"][0]
        return snap, e

    def session2(self, cookie, e):
        """Graf po restarcie: serial-e z migawki należą teraz do mikrofonu i słuchawek."""
        from helpers import load_fixture
        b = GraphBuilder(load_fixture())
        if cookie is not None:
            b.core(cookie)
        mic = next(o for o in b.objs if o.get("type") == "PipeWire:Interface:Port"
                   and o["info"]["props"].get("node.id") == 59 and o["info"]["direction"] == "output")
        hp = next(o for o in b.objs if o.get("type") == "PipeWire:Interface:Port"
                  and o["info"]["props"].get("node.id") == 58 and o["info"]["direction"] == "input"
                  and o["info"]["props"].get("port.name") == "playback_FL")
        for o in b.objs:  # zwolnij serial-e, żeby nie było duplikatów
            pr = (o.get("info") or {}).get("props") or {}
            if pr.get("object.serial") in (e["out_port_serial"], e["in_port_serial"]):
                pr["object.serial"] = 900000 + o["id"]
        mic["info"]["props"]["object.serial"] = e["out_port_serial"]
        hp["info"]["props"]["object.serial"] = e["in_port_serial"]
        return b

    def test_other_pipewire_instance_is_ignored(self):
        snap, e = self.session1(111)
        b2 = self.session2(222, e)
        gr2 = g(b2)
        self.assertEqual(gr2.port_by_serial[e["out_port_serial"]].node_id, 59)
        rplan = plan_restore(gr2, snap)
        self.assertEqual(rplan.links, [])
        self.assertTrue(any("innego uruchomienia" in n for n in rplan.notes))
        self.assertEqual(snapshot_originals(gr2, snap), set())

    def test_strict_validation_even_without_instance_id(self):
        """Nawet bez cookie (stary PipeWire / brak Core) nie łączymy portów, które
        nie zgadzają się z migawką co do węzła przechwytującego i nazw."""
        snap, e = self.session1(111)
        snap["pw_instance"] = None
        b2 = self.session2(None, e)
        gr2 = g(b2)
        self.assertIsNone(gr2.instance_id)
        rplan = plan_restore(gr2, snap)
        self.assertEqual(rplan.links, [])
        self.assertEqual(snapshot_originals(gr2, snap), set())

    def test_instance_mismatch_alone_blocks_restore(self):
        """Nawet gdy po restarcie serial-e i nazwy przypadkiem się zgadzają, migawka
        z innego uruchomienia PipeWire nie jest używana."""
        b, ns = per_app()
        b.core(1)
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = TestRestore._snapshot_from(None, gr, plan)
        run_plan(b, plan)
        self.assertEqual(len(plan_restore(g(b), snap).links), 2)
        b.core(2)
        self.assertEqual(plan_restore(g(b), snap).links, [])
        self.assertEqual(snapshot_originals(g(b), snap), set())

    def test_same_instance_but_names_differ(self):
        b, ns = per_app()
        b.core(5)
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = TestRestore._snapshot_from(None, gr, plan)
        run_plan(b, plan)
        cap = snap["captures"][str(ns.cap_ff.serial)]
        for bad in ({"out_port_name": "output_XX"}, {"out_node_name": "inny"}, {"in_port_name": "input_XX"}):
            s2 = {"version": 2, "pw_instance": snap["pw_instance"], "captures": {
                str(ns.cap_ff.serial): {"node_name": cap["node_name"],
                                        "links": [dict(e, **bad) for e in cap["links"]]}}}
            self.assertEqual(plan_restore(g(b), s2).links, [], bad)
        s3 = {"version": 2, "pw_instance": snap["pw_instance"], "captures": {
            str(ns.cap_ff.serial): {"node_name": "inny węzeł", "links": cap["links"]}}}
        self.assertEqual(plan_restore(g(b), s3).links, [])
        # a poprawna migawka działa
        self.assertEqual(len(plan_restore(g(b), snap).links), 2)

    def test_snapshot_entry_must_end_in_capture_node(self):
        """Wpis, którego port wejściowy należy do innego węzła niż klucz migawki, jest pomijany."""
        b, ns = per_app()
        gr = g(b)
        plan = plan_apply(gr, detect(gr, CFG).group_nodes, ["Warframe.x64.exe"], CFG)
        snap = TestRestore._snapshot_from(None, gr, plan)
        run_plan(b, plan)
        cap = snap["captures"].pop(str(ns.cap_ff.serial))
        snap["captures"][str(ns.cap_wf.serial)] = {"node_name": "discord_capture", "links": cap["links"]}
        self.assertEqual(plan_restore(g(b), snap).links, [])


if __name__ == "__main__":
    unittest.main()
