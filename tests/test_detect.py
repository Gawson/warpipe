"""Capture detection on synthetic graphs and real (anonymized) pw-dump snapshots."""

import unittest

from helpers import GraphBuilder, load_fixture
from scenarios import (ALSA_SINK, CFG_MON, base_system, monitor_mode, per_app,
                       with_apps)

from warpipe.graph import Graph
from warpipe.planner import (KIND_MIC, KIND_SCREENSHARE, KIND_UNKNOWN,
                                    STATUS_AMBIGUOUS, STATUS_FOUND,
                                    STATUS_INVALID, STATUS_NONE, DetectConfig,
                                    app_key, detect, list_apps, plan_apply)

CFG = DetectConfig()


def g(b):
    return Graph.from_dump(b.dump())


class TestDetectPerApp(unittest.TestCase):
    """(a) oficjalny Discord: discord_capture na każdą aplikację."""

    def test_group_has_both_captures_and_own_apps(self):
        b, ns = per_app()
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual({c.id for c in det.group}, {ns.cap_wf.id, ns.cap_ff.id})
        own = {c.id: c.own_app.id for c in det.group}
        self.assertEqual(own[ns.cap_wf.id], ns.warframe.id)
        self.assertEqual(own[ns.cap_ff.id], ns.firefox.id)
        self.assertTrue(all(c.strong for c in det.group))

    def test_mic_recstream_never_in_group(self):
        """(d) recStream z rozmowy obok — nie może trafić do grupy."""
        b, ns = per_app()
        det = detect(g(b), CFG)
        self.assertNotIn(ns.rec.id, {c.id for c in det.group})
        rec = next(c for c in det.candidates if c.id == ns.rec.id)
        self.assertEqual(rec.kind, KIND_MIC)

    def test_capture_without_links_yet_found_by_name(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        cap = b.capture(target=ns.firefox.serial)
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [cap.id])


class TestDetectMonitor(unittest.TestCase):
    """(b)/(c) tryb „monitor”: po jednorazowym potwierdzeniu (CFG_MON) wykrywany sam."""

    def test_easyeffects_monitor(self):
        """(b) strumień czyta monitor easyeffects_sink."""
        b, ns = monitor_mode("ee_sink")
        det = detect(g(b), CFG_MON)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [ns.cap.id])
        self.assertEqual(det.group[0].kind, KIND_SCREENSHARE)
        self.assertFalse(det.group[0].strong)
        self.assertIsNone(det.group[0].own_app)

    def test_alsa_monitor(self):
        """(c) strumień czyta monitor wyjścia ALSA."""
        b, ns = monitor_mode("alsa")
        det = detect(g(b), CFG_MON)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [ns.cap.id])
        self.assertIn("input from an output monitor", det.group[0].reasons)

    def test_monitor_links_without_target_props(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        cap = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, cap, src_prefix="monitor")
        det = detect(g(b), CFG_MON)
        self.assertEqual([c.id for c in det.group], [cap.id])

    def test_monitor_only_needs_confirmation(self):
        """Bez potwierdzenia sam monitor wyjścia NIE wystarcza (może to być mikrofon)."""
        for sink in ("ee_sink", "alsa"):
            b, ns = monitor_mode(sink)
            det = detect(g(b), CFG)
            self.assertEqual(det.status, STATUS_AMBIGUOUS, sink)
            self.assertEqual(det.group, [])
            c = next(c for c in det.candidates if c.id == ns.cap.id)
            self.assertEqual(c.kind, KIND_SCREENSHARE)
            self.assertTrue(c.needs_confirm)
            self.assertIn("microphone", det.message)

    def test_confirmation_is_per_sink(self):
        """Potwierdzenie dla monitora ALSA nie obejmuje innego wyjścia."""
        b, ns = monitor_mode("ee_sink")
        cfg = DetectConfig(confirmed=(("Discord", "Discord", ALSA_SINK),))
        self.assertEqual(detect(g(b), cfg).status, STATUS_AMBIGUOUS)

    def test_app_stream_links_are_strong_enough(self):
        """Połączenia ze strumieni aplikacji (np. po naszej zmianie) to mocny dowód."""
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        cap = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.firefox, cap)
        det = detect(g(b), CFG)
        self.assertEqual([c.id for c in det.group], [cap.id])
        self.assertFalse(det.group[0].needs_confirm)


class TestVesktopMicOnMonitor(unittest.TestCase):
    """Wirtualny mikrofon/soundboard: mikrofon Vesktopa czyta monitor wyjścia
    (bez znaczników recStream/WEBRTC). Nie wolno go wybrać po cichu."""

    def build(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        vsink = b.sink("virtual_mic_sink")
        vcl = b.client("vesktop", "vesktop.bin")
        mic = b.capture(node_name="vesktop", app_name="vesktop", binary="vesktop.bin",
                        media_name="RecordStream", client=vcl, target=vsink.name, capture_sink=True)
        b.connect(vsink, mic, src_prefix="monitor")
        return b, ns, vsink, mic

    def test_not_chosen_automatically(self):
        b, ns, vsink, mic = self.build()
        gr = g(b)
        det = detect(gr, CFG)
        self.assertEqual(det.status, STATUS_AMBIGUOUS)
        self.assertEqual(det.group, [])
        c = next(c for c in det.candidates if c.id == mic.id)
        self.assertTrue(c.needs_confirm)
        self.assertEqual(c.sinks, ["virtual_mic_sink"])

    def test_confirmation_for_other_sink_does_not_apply(self):
        b, ns, vsink, mic = self.build()
        cfg = DetectConfig(confirmed=(("vesktop.bin", "vesktop", ALSA_SINK),))
        self.assertEqual(detect(g(b), cfg).status, STATUS_AMBIGUOUS)

    def test_explicit_confirmation_accepts(self):
        b, ns, vsink, mic = self.build()
        cfg = DetectConfig(confirmed=(("vesktop.bin", "vesktop", "virtual_mic_sink"),))
        det = detect(g(b), cfg)
        self.assertEqual([c.id for c in det.group], [mic.id])


class TestDetectMic(unittest.TestCase):
    def test_only_mic_means_none(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_NONE)
        self.assertEqual(det.group, [])
        self.assertEqual([c.kind for c in det.candidates], [KIND_MIC])

    def test_mic_without_name_markers_detected_by_links(self):
        b = GraphBuilder()
        ns = base_system(b)
        other = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream",
                          target=ns.mic.name)
        b.connect(ns.mic, other)
        det = detect(g(b), CFG)
        c = next(c for c in det.candidates if c.id == other.id)
        self.assertEqual(c.kind, KIND_MIC)
        self.assertEqual(det.status, STATUS_NONE)

    def test_mic_marker_wins_even_if_linked_from_monitor(self):
        b = GraphBuilder()
        ns = base_system(b)
        b.connect(ns.alsa, ns.rec, src_prefix="monitor")
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_NONE)

    def test_override_on_mic_is_refused(self):
        b, ns = per_app()
        det = detect(g(b), CFG, [ns.rec.serial])
        self.assertEqual(det.status, STATUS_INVALID)
        self.assertEqual(det.group, [])

    def test_discord_capture_fell_back_to_mic_is_flagged(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        cap = b.capture(target=999999)  # aplikacja zniknęła
        b.link(ns.mic.p("capture_MONO"), cap.p("input_FL"))
        b.link(ns.mic.p("capture_MONO"), cap.p("input_FR"))
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertTrue(det.group[0].mic_linked)
        self.assertTrue(any("microphone" in w for w in det.warnings))
        # enforcement usuwa mikrofon z dźwięku streamu
        plan = plan_apply(g(b), det.group_nodes, ["Firefox"], CFG)
        unl = {(o.out_port, o.in_port) for o in plan.unlinks}
        self.assertIn((ns.mic.p("capture_MONO"), cap.p("input_FL")), unl)

    def test_muted_capture_warning(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        b.capture(target=ns.firefox.serial, params={"Props": [{"mute": False, "softMute": True}]})
        det = detect(g(b), CFG)
        self.assertTrue(any("muted" in w for w in det.warnings))


class TestAmbiguous(unittest.TestCase):
    def test_two_generic_candidates(self):
        """(h) dwa strumienie bez nazw discord_capture, oba z monitora → niepewne."""
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        c1 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        c2 = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, c1, src_prefix="monitor")
        b.connect(ns.ee_sink, c2, src_prefix="monitor")
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_AMBIGUOUS)
        self.assertEqual(det.group, [])
        self.assertEqual({c.id for c in det.candidates if c.kind == KIND_SCREENSHARE}, {c1.id, c2.id})
        det2 = detect(g(b), CFG, [c2.serial])
        self.assertEqual(det2.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det2.group], [c2.id])

    def test_unknown_only_is_ambiguous_not_guessed(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        u = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_AMBIGUOUS)
        self.assertEqual(det.group, [])
        self.assertEqual(next(c for c in det.candidates if c.id == u.id).kind, KIND_UNKNOWN)

    def test_known_serial_resolves_unknown_but_not_mic(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        u = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        det = detect(g(b), CFG, None, [u.serial])
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [u.id])
        # rozpoznany wcześniej, ale teraz podłączony do mikrofonu → nie zgadujemy
        b.link(ns.mic.p("capture_MONO"), u.p("input_FL"))
        det = detect(g(b), CFG, None, [u.serial])
        self.assertEqual(det.status, STATUS_AMBIGUOUS)
        # a recStream nigdy, nawet „znany”
        det = detect(g(b), CFG, None, [ns.rec.serial])
        self.assertNotIn(ns.rec.id, [c.id for c in det.group])

    def test_strong_names_beat_generic(self):
        b, ns = per_app()
        gcap = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
        b.connect(ns.alsa, gcap, src_prefix="monitor")
        det = detect(g(b), CFG)
        self.assertEqual({c.id for c in det.group}, {ns.cap_wf.id, ns.cap_ff.id})


class TestApps(unittest.TestCase):
    def test_app_keys_and_discord_excluded(self):
        """(g) playStream Discorda nie jest aplikacją do wyboru."""
        b, ns = per_app()
        apps = list_apps(g(b), CFG)
        keys = [a.key for a in apps]
        self.assertEqual(sorted(keys, key=str.casefold), ["Firefox", "java", "Warframe.x64.exe"])
        self.assertNotIn("WEBRTC VoiceEngine", keys)
        java = next(a for a in apps if a.key == "java")
        self.assertFalse(java.has_pid)

    def test_discord_playstream_never_linked_even_if_selected(self):
        b, ns = per_app()
        plan = plan_apply(g(b), [Graph.from_dump(b.dump()).nodes[ns.cap_wf.id]], ["WEBRTC VoiceEngine"], CFG)
        self.assertFalse(any(o.out_node == ns.play.id for o in plan.links))

    def test_playstream_excluded_even_with_narrowed_match_terms(self):
        """(g) zawężone capture_match_terms nie mogą odsłonić playStream Discorda."""
        b, ns = per_app()
        cfg = DetectConfig(match_terms=("discord_capture",))
        gr = g(b)
        det = detect(gr, cfg)
        self.assertTrue(det.found)
        keys = [a.key for a in list_apps(gr, cfg)]
        self.assertEqual(sorted(keys, key=str.casefold), ["Firefox", "java", "Warframe.x64.exe"])
        plan = plan_apply(gr, det.group_nodes, ["WEBRTC VoiceEngine"], cfg)
        self.assertFalse(any(o.out_node == ns.play.id for o in plan.ops if o.kind == "link"))

    def test_vesktop_playback_excluded_with_custom_terms(self):
        b = GraphBuilder()
        b.app("Firefox", "firefox")
        b.app("vesktop", "vesktop.bin", node_name="vesktop")
        cfg = DetectConfig(match_terms=("something-else",))
        self.assertEqual([a.key for a in list_apps(g(b), cfg)], ["Firefox"])

    def test_generic_loader_key(self):
        b = GraphBuilder()
        a1 = b.app("wine64-preloader", "wine64-preloader", node_name="Game.exe")
        a2 = b.app(None, "python3", node_name="python3")
        a3 = b.app("", None, node_name="mpv")
        gr = g(b)
        self.assertEqual(app_key(gr.nodes[a1.id]), "Game.exe")
        self.assertEqual(app_key(gr.nodes[a2.id]), "python3")
        self.assertEqual(app_key(gr.nodes[a3.id]), "mpv")

    def test_venmic_loopbacks_excluded(self):
        b = GraphBuilder()
        b.app("Firefox", "firefox")
        b.node("Stream/Output/Audio", "venmic-loopback-playback-55-60", outs=("FL", "FR"))
        keys = [a.key for a in list_apps(g(b), CFG)]
        self.assertEqual(keys, ["Firefox"])


class TestVesktop(unittest.TestCase):
    def test_vesktop_record_from_virtmic_is_screenshare_mic_is_not(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        vss = b.node("Audio/Source/Virtual", "vencord-screen-share", ins=("FL", "FR"), outs=("FL", "FR"),
                     out_prefix="capture")
        vcl = b.client("vesktop", "vesktop.bin")
        share = b.capture(node_name="vesktop", app_name="vesktop", binary="vesktop.bin",
                          media_name="RecordStream", client=vcl, target=vss.name)
        b.connect(vss, share)
        mic = b.capture(node_name="vesktop", app_name="vesktop", binary="vesktop.bin",
                        media_name="RecordStream", client=vcl, target=ns.mic.name)
        b.connect(ns.mic, mic)
        det = detect(g(b), CFG)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [share.id])
        self.assertEqual(next(c for c in det.candidates if c.id == mic.id).kind, KIND_MIC)


class TestSampleNoAudioFixture(unittest.TestCase):
    """Prawdziwy (zanonimizowany) zrzut z pulpitu KDE/Wayland: udostępnianie ekranu bez dźwięku."""

    def setUp(self):
        self.objs = load_fixture()
        self.graph = Graph.from_dump(self.objs)

    def test_no_capture_and_recstream_not_picked(self):
        det = detect(self.graph, CFG)
        self.assertEqual(det.status, STATUS_NONE)
        self.assertEqual(det.group, [])
        mic = next(c for c in det.candidates if c.id == 93)
        self.assertEqual(mic.kind, KIND_MIC)
        self.assertEqual(len(det.candidates), 1)

    def test_apps(self):
        keys = [a.key for a in list_apps(self.graph, CFG)]
        self.assertEqual(keys, ["Firefox", "java"])
        java = next(a for a in list_apps(self.graph, CFG) if a.key == "java")
        self.assertFalse(java.has_pid)

    def test_with_synthetic_discord_capture(self):
        """Dokładamy discord_capture dla Firefoksa (tak jak zrobiłby to Discord ze dźwiękiem)."""
        b = GraphBuilder(self.objs)
        ff = self.graph.nodes[177]
        cap = b.capture(target=ff.serial, client=159)
        b.link(172, cap.p("input_FL"))
        b.link(142, cap.p("input_FR"))
        gr = Graph.from_dump(b.dump())
        det = detect(gr, CFG)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [cap.id])
        self.assertEqual(det.group[0].own_app.id, 177)
        plan = plan_apply(gr, det.group_nodes, ["java"], CFG)
        self.assertEqual({(o.out_port, o.in_port) for o in plan.unlinks},
                         {(172, cap.p("input_FL")), (142, cap.p("input_FR"))})
        self.assertEqual({(o.out_port, o.in_port) for o in plan.links},
                         {(165, cap.p("input_FL")), (146, cap.p("input_FR"))})
        # niczego, co nie kończy się w discord_capture
        self.assertTrue(all(o.in_node == cap.id for o in plan.ops))


class TestSampleGoLiveCapture(unittest.TestCase):
    """Prawdziwy (zanonimizowany) zrzut z pulpitu KDE/Wayland: Go Live Z DŹWIĘKIEM.

    discord_capture #179 (serial 1273, target.object 1272 = Firefox #221);
    Firefox gra jednocześnie do easyeffects_sink i do discord_capture;
    mikrofon rozmowy „WEBRTC VoiceEngine” (recStream #93) czyta easyeffects_source;
    playStream #191 to głosy z rozmowy."""

    CAP, FIREFOX, MIC, PLAY = 179, 221, 93, 191

    def setUp(self):
        from helpers import SAMPLE_GOLIVE
        self.objs = load_fixture(SAMPLE_GOLIVE)
        self.graph = Graph.from_dump(self.objs)

    def test_detect_capture_not_mic(self):
        det = detect(self.graph, CFG)
        self.assertEqual(det.status, STATUS_FOUND)
        self.assertEqual([c.id for c in det.group], [self.CAP])
        cap = det.group[0]
        self.assertTrue(cap.strong)
        self.assertEqual(cap.serial, 1273)
        self.assertEqual(cap.own_app.id, self.FIREFOX)
        self.assertEqual(cap.own_app.serial, 1272)
        mic = next(c for c in det.candidates if c.id == self.MIC)
        self.assertEqual(mic.kind, KIND_MIC)
        self.assertNotIn(self.MIC, [c.id for c in det.group])
        self.assertEqual(det.warnings, [])

    def test_firefox_in_stream_playstream_not_an_app(self):
        from warpipe.planner import apps_in_stream
        det = detect(self.graph, CFG)
        apps = list_apps(self.graph, CFG)
        self.assertEqual([a.key for a in apps], ["Firefox"])
        self.assertNotIn(self.PLAY, [n.id for a in apps for n in a.nodes])
        self.assertEqual(apps_in_stream(self.graph, det.group_nodes, apps), ["Firefox"])
        inbound = {(l.out_node, self.graph.ports[l.out_port].name, self.graph.ports[l.in_port].name)
                   for l in self.graph.links_into(self.CAP)}
        self.assertEqual(inbound, {(self.FIREFOX, "output_FL", "input_FL"),
                                   (self.FIREFOX, "output_FR", "input_FR")})

    def test_firefox_selected_means_no_changes(self):
        det = detect(self.graph, CFG)
        plan = plan_apply(self.graph, det.group_nodes, ["Firefox"], CFG)
        self.assertEqual(plan.ops, [])

    def test_apply_and_restore_only_touch_links_into_capture(self):
        """Enforcer na zrzucie: wybór bez Firefoksa odcina go od #179, „Cały system” przywraca.
        Żadna operacja nie dotyka niczego poza wejściami #179 (mikrofon, easyeffects nietknięte)."""
        import tempfile
        from pathlib import Path

        from helpers import FakePw

        from warpipe.enforcer import Enforcer
        from warpipe.state import Config, SnapshotStore
        b = GraphBuilder(self.objs)
        before = b.all_link_pairs()
        cap_in_ports = {o["id"] for o in b.objs if o.get("type") == "PipeWire:Interface:Port"
                        and o["info"]["props"].get("node.id") == self.CAP}
        with tempfile.TemporaryDirectory() as d:
            fake = FakePw(b)
            enf = Enforcer(fake, Config(Path(d) / "c.json"), SnapshotStore(Path(d) / "s.json"),
                           enabled=True, selected=["Spotify"], persist=False)
            st = enf.refresh()
            self.assertIsNone(st.error)
            self.assertEqual(st.in_stream, [])
            self.assertEqual(Graph.from_dump(b.dump()).links_into(self.CAP), [])
            self.assertEqual(len(fake.mutations), 2)
            ok, msg = enf.restore()
            self.assertTrue(ok, msg)
            self.assertEqual(b.all_link_pairs(), before)
            st = enf.refresh()
            self.assertEqual(st.in_stream, [])  # nadal włączone z pustym wyborem → odcina znowu
            enf.set_enabled(False)
            self.assertEqual(b.all_link_pairs(), before)
            self.assertEqual(enf.status.in_stream, ["Firefox"])
        self.assertTrue(fake.mutations)
        for m in fake.mutations:
            self.assertIn(m[2], cap_in_ports, m)   # tylko połączenia DO discord_capture #179


if __name__ == "__main__":
    unittest.main()
