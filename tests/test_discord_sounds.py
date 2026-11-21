"""Discord's own sounds (notification dings) kept off the stream — also in "Whole system" mode.

Discord plays its dings from the app process (client application.name "discord", its own
PID), not from the voice engine, so it opens a discord_capture for them and streams them.
Warpipe cuts only those links; it never changes PipeWire properties."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import FakePw, GraphBuilder
from scenarios import base_system, per_app, with_apps
from test_enforcer import add_ding
from test_monitor import FakeMonPw, arr, wait_for

from warpipe.enforcer import Enforcer, Watcher
from warpipe.graph import Graph
from warpipe.planner import (DetectConfig, app_streams, is_discord_sound_stream, list_apps, plan_apply,
                             plan_cut_discord_sounds, plan_restore)
from warpipe.state import Config, SnapshotStore

ON = DetectConfig()
OFF = DetectConfig(mute_discord_own_sounds=False)


def real_ding(b, cap_target=True):
    """As seen on a real desktop: client application.name "discord" (pipewire-pulse, its
    own PID), a discord_capture for it, WirePlumber's links."""
    n0 = len(b.objs)
    ding = b.app("discord", "Discord", node_name="Chromium", media_name="Playback")
    cap = b.capture(target=ding.serial) if cap_target else None
    links = b.connect(ding, cap) if cap is not None else []
    return ding, cap, links, b.objs[n0:]


class TestMatcher(unittest.TestCase):
    def node(self, b, ref):
        return Graph.from_dump(b.dump()).nodes[ref.id]

    def test_discord_app_streams(self):
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        ding, _cap, _l, _ = real_ding(b)
        old_style = b.app("Discord", "Discord", node_name="Discord", media_name="notification")
        canary = b.app("discordcanary", "DiscordCanary")
        flatpak = b.app("Chromium", "chromium", extra={"pipewire.access.portal.app_id": "com.discordapp.Discord"})
        for ref in (ding, old_style, canary, flatpak):
            self.assertTrue(is_discord_sound_stream(self.node(b, ref), ON), ref)
        # the voice engine (call voices, microphone), helpers and other apps: never
        hw = b.app("discord_hardware_mute", "Discord")
        webrtc = b.app("discord", "Discord", node_name="webrtc-audio")
        play_named = b.app("discord", "Discord", media_name="playStream")
        lookalike = b.app("discordant-player", "discordant")
        vesktop = b.app("vesktop", "vesktop")
        venmic = b.app("discord", "Discord", node_name="vencord-sink")
        for ref in (ns.play, ns.rec, hw, webrtc, play_named, ns.firefox, ns.warframe, ns.java, lookalike,
                    vesktop, venmic):
            self.assertFalse(is_discord_sound_stream(self.node(b, ref), ON), ref)

    def test_names_from_the_config(self):
        b = GraphBuilder()
        mine = b.app("my-discord-build", "mdb")
        ding, _c, _l, _ = real_ding(b)
        cfg = DetectConfig(discord_sound_names=("My-Discord-Build",))
        self.assertTrue(is_discord_sound_stream(self.node(b, mine), cfg))
        self.assertFalse(is_discord_sound_stream(self.node(b, ding), cfg))
        cfg = DetectConfig(mic_names=("WEBRTC VoiceEngine", "Chromium"))   # the user's voice marker wins
        self.assertFalse(is_discord_sound_stream(self.node(b, ding), cfg))

    def test_config_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            self.assertTrue(Config(p).mute_discord_own_sounds)               # default on
            self.assertTrue(Config(p).detect_config().mute_discord_own_sounds)
            p.write_text(json.dumps({"mute_discord_own_sounds": False, "discord_sound_names": ["x", ""]}))
            cfg = Config(p).detect_config()
            self.assertFalse(cfg.mute_discord_own_sounds)
            self.assertEqual(cfg.discord_sound_names, ("x",))
            p.write_text(json.dumps({"mute_discord_own_sounds": "false", "discord_sound_names": "x"}))
            cfg = Config(p).detect_config()
            self.assertTrue(cfg.mute_discord_own_sounds)                     # junk = default
            self.assertIn("discord", cfg.discord_sound_names)


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.b, self.ns = per_app()
        self.ding, self.cap, self.links, _ = real_ding(self.b)
        self.g = Graph.from_dump(self.b.dump())

    def group(self, g=None):
        from warpipe.planner import detect
        g = g or self.g
        return detect(g, ON).group_nodes

    def test_app_list(self):
        self.assertNotIn("discord", [a.key for a in list_apps(self.g, ON)])
        self.assertIn("discord", [a.key for a in list_apps(self.g, OFF)])        # selectable when allowed
        # the voice engine is never an app, with or without the option
        self.assertNotIn(self.ns.play.id, [n.id for n in app_streams(self.g, OFF)])

    def test_only_selected_never_links_it_even_if_selected_earlier(self):
        plan = plan_apply(self.g, self.group(), ["Warframe.x64.exe", "discord"], ON)
        cut = {(o.out_node, o.in_node) for o in plan.unlinks}
        self.assertIn((self.ding.id, self.cap.id), cut)
        self.assertNotIn(self.ding.id, {o.out_node for o in plan.links})
        plan = plan_apply(self.g, self.group(), ["Warframe.x64.exe", "discord"], OFF)
        self.assertNotIn((self.ding.id, self.cap.id), {(o.out_node, o.in_node) for o in plan.unlinks})

    def test_whole_system_cut_touches_only_discord_sounds(self):
        mic_link = self.b.link(self.ding.p("output_FL"), self.ns.rec.p("input_FL"))   # into the call mic
        g = Graph.from_dump(self.b.dump())
        plan = plan_cut_discord_sounds(g, self.group(g), ON)
        self.assertEqual(len(plan.ops), 2)
        self.assertTrue(all(o.kind == "unlink" and o.out_node == self.ding.id and o.in_node == self.cap.id
                            for o in plan.ops))
        self.assertNotIn(mic_link, {o.link_id for o in plan.ops})
        self.assertEqual(plan_cut_discord_sounds(g, self.group(g), OFF).ops, [])
        self.assertEqual(plan_cut_discord_sounds(g, [], ON).ops, [])

    def test_restore_keeps_them_cut_while_the_option_is_on(self):
        from warpipe.planner import link_entry
        g = self.g
        snap = {"version": 2, "pw_instance": g.instance_id, "captures": {}}
        for cap in (self.cap, self.ns.cap_ff):
            entries = [link_entry(g, l, deleted=True) for l in g.links_into(cap.id)]
            snap["captures"][str(cap.serial)] = {"node_name": "discord_capture", "links": entries}
        for l in list(g.links_into(self.cap.id)) + list(g.links_into(self.ns.cap_ff.id)):
            self.b.remove_link(l.out_port, l.in_port)
        g = Graph.from_dump(self.b.dump())
        plain = plan_restore(g, snap)                                      # no cfg: everything back
        self.assertEqual(len(plain.links), 4)
        kept = plan_restore(g, snap, ON)
        self.assertEqual({o.out_node for o in kept.links}, {self.ns.firefox.id})
        self.assertEqual(len(kept.kept), 2)
        self.assertEqual({k[0] for k in kept.kept}, {self.cap.serial})
        self.assertEqual(len(plan_restore(g, snap, OFF).links), 4)
        only = plan_restore(g, snap, OFF, only_discord_sounds=True)
        self.assertEqual({o.out_node for o in only.links}, {self.ding.id})
        # only_discord_sounds never removes anything else (e.g. Warpipe's own links)
        self.b.link(self.ns.warframe.p("output_FL"), self.ns.cap_ff.p("input_FL"), managed=True)
        g = Graph.from_dump(self.b.dump())
        self.assertEqual(plan_restore(g, snap, OFF, only_discord_sounds=True).unlinks, [])
        self.assertEqual(len(plan_restore(g, snap, ON).unlinks), 1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, b, **kw):
        self.fake = FakePw(b)
        kw.setdefault("on_event", lambda k, m: self.events.append((k, m)))
        return Enforcer(self.fake, Config(self.dir / "config.json"), SnapshotStore(self.dir / "snap.json"), **kw)

    def inbound(self, b, node):
        return {l.out_node for l in Graph.from_dump(b.dump()).links.values() if l.in_node == node.id}

    def link_pairs(self, b):
        return Graph.from_dump(b.dump()).link_pairs().keys()


class TestWholeSystem(Base):
    def test_only_the_ding_is_cut_everything_else_untouched(self):
        b, ns = per_app()
        enf = self.make(b)
        enf.refresh()
        before = set(self.link_pairs(b))
        ding, cap, links, _ = real_ding(b)
        st = enf.refresh("graph changed")
        self.assertFalse(st.enabled)
        self.assertTrue(st.mute_discord_own_sounds)
        self.assertEqual(self.inbound(b, cap), set())
        self.assertEqual(set(self.link_pairs(b)), before)                 # exactly Discord's own state
        self.assertEqual(self.inbound(b, ns.cap_ff), {ns.firefox.id})
        self.assertEqual([a.key for a in st.apps], ["Firefox", "java", "Warframe.x64.exe"])
        self.assertFalse(any(c[0] == "link" for c in self.fake.calls))
        # restore and quit keep it cut; the snapshot remembers it
        self.assertTrue(enf.restore("whole system")[0])
        enf.shutdown()
        self.assertEqual(self.inbound(b, cap), set())
        store = SnapshotStore(self.dir / "snap.json")
        self.assertTrue(store.captures[str(cap.serial)]["links"])
        # turning the option off (another run of Warpipe) brings it back
        enf2 = self.make(b)
        enf2.set_mute_discord_sounds(False)
        self.assertEqual(self.inbound(b, cap), {ding.id})
        self.assertFalse(Config(self.dir / "config.json").mute_discord_own_sounds)   # saved
        self.assertFalse(enf2.status.mute_discord_own_sounds)
        self.assertEqual(self.inbound(b, ns.cap_ff), {ns.firefox.id})

    def test_option_toggling_and_persistence(self):
        b, ns = per_app()
        ding, cap, links, _ = real_ding(b)
        enf = self.make(b, dcfg=OFF)
        enf.refresh()
        self.assertEqual(self.inbound(b, cap), {ding.id})                 # allowed: Discord's default
        self.assertIn("discord", [a.key for a in enf.status.apps])
        enf.set_mute_discord_sounds(True)
        self.assertEqual(self.inbound(b, cap), set())
        self.assertTrue(Config(self.dir / "config.json").mute_discord_own_sounds)
        self.assertEqual(self.events[-1], ("applied", "Discord's own sounds: kept off the stream"))
        enf.set_mute_discord_sounds(False)
        self.assertEqual(self.inbound(b, cap), {ding.id})
        # a ding stream that went away meanwhile: nothing to bring back, no error
        enf.set_mute_discord_sounds(True)
        b.remove_node(ding)
        st = enf.set_mute_discord_sounds(False)
        self.assertIsNone(st.error)

    def test_only_selected_mode_with_the_option(self):
        b, ns = per_app()
        ding, cap, links, _ = real_ding(b)
        enf = self.make(b, enabled=True, selected=["Warframe.x64.exe", "discord"], dcfg=OFF)
        enf.refresh()
        self.assertEqual(self.inbound(b, cap), {ding.id})                 # allowed + selected
        enf.set_mute_discord_sounds(True)                                 # never, even if selected
        self.assertEqual(self.inbound(b, cap), set())
        self.assertNotIn("discord", [a.key for a in enf.status.apps])
        self.assertEqual(enf.status.missing_selected(), ["discord"])
        enf.set_mute_discord_sounds(False)                                # Only selected: still the selection
        self.assertEqual(self.inbound(b, cap), {ding.id})
        enf.set_app_selected("discord", False)
        self.assertEqual(self.inbound(b, cap), set())
        enf.set_mute_discord_sounds(True)
        enf.set_enabled(False)                                            # Whole system: ding stays off
        self.assertEqual(self.inbound(b, cap), set())
        self.assertEqual(self.inbound(b, ns.cap_ff), {ns.firefox.id})

    def test_fast_cut_in_whole_system(self):
        b, ns = per_app()
        b.core(111)
        enf = self.make(b)
        enf.refresh()
        self.assertTrue(enf.cuts_new_links)
        for maker in (add_ding, real_ding):
            ding, cap, links, _ = maker(b)
            n_dumps = sum(1 for c in self.fake.calls if c[0] == "dump")
            self.assertEqual(len(enf.fast_cut(b.dump(), links)), 2)
            self.assertEqual(self.inbound(b, cap), set())
            self.assertEqual(sum(1 for c in self.fake.calls if c[0] == "dump"), n_dumps)
        # a new game stream with its new capture: not Warpipe's business in Whole system
        sp = b.app("Spotify", "spotify")
        spcap = b.capture(target=sp.serial)
        sl = b.connect(sp, spcap)
        self.assertEqual(enf.fast_cut(b.dump(), sl), [])
        self.assertEqual(self.inbound(b, spcap), {sp.id})
        # another PipeWire session, loop protection, option off: nothing
        ding, cap, links, _ = real_ding(b)
        view = b.dump()
        view[0]["info"]["cookie"] = 222
        self.assertEqual(enf.fast_cut(view, links), [])
        enf._paused_until = enf.clock() + 100
        self.assertEqual(enf.fast_cut(b.dump(), links), [])
        enf._paused_until = 0.0
        enf.dcfg = OFF
        self.assertFalse(enf.cuts_new_links)
        self.assertEqual(enf.fast_cut(b.dump(), links), [])
        self.assertEqual(self.inbound(b, cap), {ding.id})

    def test_loop_protection(self):
        """A burst of dings is not a loop; the same link coming back again and again is."""
        b, ns = per_app()
        enf = self.make(b, loop_limit=2)
        enf.refresh()
        for _i in range(5):                                               # 5 separate dings
            ding, cap, links, _ = real_ding(b)
            enf.refresh("graph changed")
            self.assertEqual(self.inbound(b, cap), set())
        self.assertFalse(enf.status.paused)
        for _i in range(4):                                               # something relinks one
            b.connect(ding, cap)
            enf.refresh("graph changed")
        self.assertTrue(enf.status.paused)
        self.assertIn("loop", [k for k, _m in self.events])
        b.connect(ding, cap)
        enf.refresh("graph changed")
        self.assertEqual(self.inbound(b, cap), {ding.id})                 # paused: left alone


class TestWatcherFastPathWholeSystem(unittest.TestCase):
    def test_ding_cut_before_the_debounced_refresh(self):
        b, ns = per_app()
        fake = FakePw(b)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        enf = Enforcer(fake, Config(d / "c.json"), SnapshotStore(d / "s.json"), enabled=False, persist=False)
        pw = FakeMonPw()
        w = Watcher(enf, pw, debounce=1.5, max_delay=3.0, poll_interval=60.0)
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: pw.procs and fake.calls))
            pw.procs[0].write(arr(b.dump()))
            self.assertTrue(wait_for(lambda: w.refresh_count >= 1))
            dumps = sum(1 for c in fake.calls if c[0] == "dump")
            ding, cap, links, new = real_ding(b)
            t0 = time.monotonic()
            pw.procs[0].write(arr(new))
            self.assertTrue(wait_for(lambda: not any(o["id"] in links for o in b.objs), 1.0))
            self.assertLess(time.monotonic() - t0, 0.5)                  # the debounce alone is 1.5 s
            self.assertEqual(sum(1 for c in fake.calls if c[0] == "dump"), dumps)
            g = Graph.from_dump(b.dump())
            self.assertEqual({l.out_node for l in g.links.values() if l.in_node == ns.cap_ff.id},
                             {ns.firefox.id})                             # Whole system: all else stays
        finally:
            w.stop()
            t.join(3)
        self.assertFalse(t.is_alive())


class TestControlAndCli(Base):
    def test_control_command_and_status(self):
        from warpipe.control import ControlHandler, parse_request, status_to_dict
        b, ns = per_app()
        ding, cap, links, _ = real_ding(b)
        enf = self.make(b)
        enf.refresh()
        h = ControlHandler(enf)
        self.assertTrue(status_to_dict(enf.status)["mute_discord_own_sounds"])
        r = h.handle(parse_request('{"cmd": "discord_sounds", "on": true}'))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "Discord's own sounds: allowed on the stream")
        self.assertFalse(r["status"]["mute_discord_own_sounds"])
        self.assertEqual(self.inbound(b, cap), {ding.id})
        r = h.handle(parse_request('{"cmd": "discord_sounds", "on": "off"}'))
        self.assertTrue(r["status"]["mute_discord_own_sounds"])
        self.assertEqual(self.inbound(b, cap), set())
        from warpipe.control import ProtocolError
        with self.assertRaises(ProtocolError):
            parse_request('{"cmd": "discord_sounds"}')

    def test_cli_without_a_running_instance(self):
        import contextlib
        import io
        import os
        from unittest import mock

        from warpipe import cli
        b, ns = per_app()
        ding, cap, links, _ = real_ding(b)
        env = {"WARPIPE_CONFIG_DIR": str(self.dir / "conf"), "WARPIPE_RUNTIME_DIR": str(self.dir / "run"),
               "WARPIPE_LANG": "en"}
        fake = FakePw(b)
        enf = Enforcer(fake, Config(self.dir / "conf" / "config.json"),
                       SnapshotStore(self.dir / "run" / "snapshot.json"))
        with mock.patch.dict(os.environ, env):
            enf.refresh()                                       # a Warpipe run cut the ding …
            enf.shutdown()                                      # … and quit
            self.assertEqual(self.inbound(b, cap), set())
            out = io.StringIO()
            with mock.patch.object(cli, "PwCli", return_value=fake), contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--discord-sounds", "on"]), 0)
            self.assertIn("allowed on the stream", out.getvalue())
            self.assertEqual(self.inbound(b, cap), {ding.id})   # brought back
            self.assertFalse(Config(self.dir / "conf" / "config.json").mute_discord_own_sounds)
            out = io.StringIO()
            with mock.patch.object(cli, "PwCli", return_value=fake), contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--discord-sounds", "off"]), 0)
                self.assertEqual(cli.main(["--list"]), 0)
            self.assertIn("Discord's own sounds: kept off the stream", out.getvalue())
            self.assertTrue(Config(self.dir / "conf" / "config.json").mute_discord_own_sounds)
            out = io.StringIO()
            with mock.patch.object(cli, "PwCli", return_value=fake), contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--list", "--json"]), 0)
            data = json.loads(out.getvalue())
            self.assertTrue(data["config"]["mute_discord_own_sounds"])
            self.assertEqual([s["node_id"] for s in data["discord_sounds"]], [ding.id])

    def test_status_text(self):
        import contextlib
        import io

        from warpipe import cli
        from warpipe.control import status_to_dict
        b, ns = per_app()
        enf = self.make(b)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_status(status_to_dict(enf.refresh()))
        self.assertIn("Discord's own sounds: kept off the stream", out.getvalue())


if __name__ == "__main__":
    unittest.main()
