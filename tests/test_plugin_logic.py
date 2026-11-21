"""OpenDeck plugin: pure logic (no Qt) — state -> appearance, key presses, settings, language."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (path to the plugin lib)
from warpipe_plugin import logic as L
from warpipe_plugin.logic import (ACTION_APP, ACTION_SYSTEM, S_ABSENT, S_DOWN,
                            S_ERROR, S_NOSTREAM, S_OFF, S_ON, S_STARTING,
                            S_SYS_OFF, S_SYS_ON, S_UNSET, S_UNSURE)

from warpipe.control import status_to_dict


def st(enforce=False, capture="active", apps=(), error=None):
    d = {"version": 1, "enforce": enforce, "capture": {"status": capture, "count": 1},
         "apps": [{"key": k, "display": k, "present": p, "selected": s, "in_stream": i} for k, p, s, i in apps]}
    if error:
        d["error"] = error
    return L.normalize_status(d)


WF = {"app": "Warframe.x64.exe", "label": ""}


class TestVisual(unittest.TestCase):
    def v(self, settings, status, running=True, starting=False):
        return L.app_visual(settings, status, running, starting)

    def test_unset(self):
        v = self.v({"app": "", "label": ""}, st())
        self.assertEqual((v.state, v.label), (S_UNSET, "pick an app"))

    def test_tool_down_and_starting(self):
        self.assertEqual(self.v(WF, None, False).state, S_DOWN)
        self.assertEqual(self.v(WF, None, False).badge, "start")
        self.assertEqual(self.v(WF, None, False, True).state, S_STARTING)
        self.assertEqual(L.system_visual(None, False).state, S_DOWN)
        self.assertEqual(L.system_visual(None, False, True).state, S_STARTING)

    def test_absent_dim_with_name(self):
        v = self.v(WF, st(apps=[("Firefox", True, False, True)]))
        self.assertEqual((v.state, v.badge, v.label, v.ring), (S_ABSENT, "not playing", "Warframe", False))
        # selected, but not playing now -> ring
        v = self.v(WF, st(True, apps=[("Warframe.x64.exe", False, True, False)]))
        self.assertEqual((v.state, v.ring), (S_ABSENT, True))

    def test_present_not_in_stream(self):
        v = self.v(WF, st(True, apps=[("Warframe.x64.exe", True, False, False)]))
        self.assertEqual((v.state, v.badge, v.ring), (S_OFF, "", False))

    def test_in_stream_lit(self):
        v = self.v(WF, st(False, apps=[("warframe.X64.exe", True, False, True)]))  # case-insensitive
        self.assertEqual((v.state, v.badge), (S_ON, "on stream"))

    def test_no_stream_neutral_hint(self):
        v = self.v(WF, st(False, "none", [("Warframe.x64.exe", True, False, False)]))
        self.assertEqual((v.state, v.badge, v.ring), (S_NOSTREAM, "no stream", False))
        v = self.v(WF, st(True, "none", [("Warframe.x64.exe", True, True, False)]))
        self.assertEqual((v.state, v.ring), (S_NOSTREAM, True))
        # "selected" without enforcement is a dormant choice — no ring
        v = self.v(WF, st(False, "none", [("Warframe.x64.exe", True, True, False)]))
        self.assertFalse(v.ring)

    def test_ambiguous_and_error(self):
        self.assertEqual(self.v(WF, st(capture="ambiguous", apps=[("Warframe.x64.exe", True, False, False)])).state,
                         S_UNSURE)
        self.assertEqual(self.v(WF, st(capture="none", error="pw-dump")).state, S_ERROR)

    def test_custom_label(self):
        v = self.v({"app": "Warframe.x64.exe", "label": "WF"}, st())
        self.assertEqual(v.label, "WF")

    def test_system_key(self):
        self.assertEqual(L.system_visual(st(False), True).state, S_SYS_ON)
        self.assertEqual(L.system_visual(st(True), True).state, S_SYS_OFF)
        self.assertEqual(L.system_visual(st(True), True).label, "Whole system")
        self.assertEqual(L.visual_for(ACTION_SYSTEM, {}, st(False), True).state, S_SYS_ON)

    def test_visual_hashable_and_tag(self):
        v = self.v(WF, st())
        self.assertEqual(v.tag, f"app:{v.state}")
        self.assertEqual(len({v, self.v(WF, st())}), 1)

    def test_with_real_tool_status(self):
        """A status from the real tool (status_to_dict) passes normalize_status."""
        from helpers import FakePw
        from scenarios import per_app

        from warpipe.enforcer import Enforcer
        from warpipe.state import Config, SnapshotStore
        b, ns = per_app()
        with tempfile.TemporaryDirectory() as d:
            enf = Enforcer(FakePw(b), Config(Path(d) / "c.json"), SnapshotStore(Path(d) / "s.json"),
                           enabled=True, selected=["java"], persist=False)
            enf.refresh()
            status = L.normalize_status(json.loads(json.dumps(status_to_dict(enf.status))))
        self.assertEqual(L.app_visual({"app": "java", "label": ""}, status, True).state, S_ON)
        self.assertEqual(L.app_visual(WF, status, True).state, S_OFF)
        self.assertEqual(L.app_visual({"app": "Spotify", "label": ""}, status, True).state, S_ABSENT)
        self.assertEqual(L.system_visual(status, True).state, S_SYS_OFF)


class TestPress(unittest.TestCase):
    def test_launch_when_down(self):
        self.assertEqual(L.press_for(ACTION_APP, WF, None, False).kind, "launch")
        self.assertEqual(L.press_for(ACTION_SYSTEM, {}, None, False).kind, "launch")
        self.assertEqual(L.press_for(ACTION_APP, WF, None, False, starting=True).kind, "none")

    def test_toggle_present(self):
        p = L.press_for(ACTION_APP, WF, st(apps=[("Warframe.x64.exe", True, False, True)]), True)
        self.assertEqual((p.kind, p.app), ("toggle", "Warframe.x64.exe"))

    def test_toggle_allowed_without_stream(self):
        p = L.press_for(ACTION_APP, WF, st(capture="none", apps=[("Warframe.x64.exe", True, False, False)]), True)
        self.assertEqual(p.kind, "toggle")

    def test_absent_alerts_unless_selected(self):
        p = L.press_for(ACTION_APP, WF, st(apps=[]), True)
        self.assertEqual(p.kind, "alert")
        self.assertIn("is not playing right now", p.reason)
        p = L.press_for(ACTION_APP, WF, st(True, apps=[("Warframe.x64.exe", False, True, False)]), True)
        self.assertEqual(p.kind, "toggle")  # a remembered app can be unselected

    def test_unset_and_system(self):
        self.assertEqual(L.press_for(ACTION_APP, {"app": "", "label": ""}, st(), True).kind, "alert")
        self.assertEqual(L.press_for(ACTION_SYSTEM, {}, st(), True).kind, "system")


class TestSettings(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(L.sanitize_settings(None), {"app": "", "label": ""})
        self.assertEqual(L.sanitize_settings({"app": "  Gra\x00\n.exe ", "label": 5, "x": 1}),
                         {"app": "Gra.exe", "label": ""})
        self.assertEqual(len(L.sanitize_settings({"app": "a" * 1000})["app"]), 200)
        self.assertEqual(L.sanitize_global_settings({"toolPath": ["x"]}), {"toolPath": ""})

    def test_short_label(self):
        self.assertEqual(L.short_label("Warframe.x64.exe"), "Warframe")
        self.assertEqual(L.short_label("Game-Win64.EXE"), "Game")
        self.assertEqual(L.short_label("Firefox"), "Firefox")
        self.assertEqual(L.short_label(".exe"), ".exe")
        self.assertEqual(L.short_label("java"), "java")

    def test_normalize_status_rejects_junk(self):
        self.assertIsNone(L.normalize_status("x"))
        s = L.normalize_status({"enforce": "true", "capture": {"status": "boom", "count": True},
                                "apps": [1, {"key": ""}, {"key": "A", "present": 1, "in_stream": True}]})
        self.assertEqual(s["enforce"], False)
        self.assertEqual(s["capture"], {"status": "none", "count": 0})
        self.assertEqual(s["apps"], [{"key": "A", "display": "A", "present": False, "selected": False,
                                      "in_stream": True}])

    def test_apps_for_pi(self):
        s = st(True, apps=[("Zed", False, True, False), ("Firefox", True, False, True), ("alpha", True, True, False)])
        p = L.apps_for_pi(s, True, WF)
        self.assertEqual(p["type"], "apps")
        self.assertTrue(p["toolRunning"])
        self.assertEqual([a["key"] for a in p["apps"]], ["alpha", "Firefox", "Zed"])  # playing apps first
        self.assertEqual(p["current"], WF)
        self.assertEqual(p["lang"], "en")
        self.assertEqual(L.apps_for_pi(None, False, WF)["apps"], [])
        self.assertFalse(L.apps_for_pi(None, False, WF)["toolRunning"])

    def test_socket_path(self):
        self.assertEqual(L.control_socket_path({"WARPIPE_RUNTIME_DIR": "/x/y"}), "/x/y/control.sock")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(L.control_socket_path({"XDG_RUNTIME_DIR": d}),
                             os.path.join(d, "warpipe", "control.sock"))
        # same as the tool
        from warpipe.control import socket_path
        self.assertEqual(L.control_socket_path(), str(socket_path()))


class TestLanguage(unittest.TestCase):
    def tearDown(self):
        L.set_language("en")

    def test_resolve_env_override(self):
        self.assertEqual(L.resolve_language({"WARPIPE_LANG": "pl", "LANG": "en_US.UTF-8"}), "pl")
        self.assertEqual(L.resolve_language({"WARPIPE_LANG": "en", "LANG": "pl_PL.UTF-8"}), "en")
        self.assertEqual(L.resolve_language({"WARPIPE_LANG": "auto", "LANG": "pl_PL.UTF-8"}), "pl")

    def test_resolve_locale(self):
        with tempfile.TemporaryDirectory() as d:
            base = {"WARPIPE_CONFIG_DIR": d}
            self.assertEqual(L.resolve_language(dict(base, LANG="pl_PL.UTF-8")), "pl")
            self.assertEqual(L.resolve_language(dict(base, LC_ALL="en_GB.UTF-8", LANG="pl_PL.UTF-8")), "en")
            self.assertEqual(L.resolve_language(dict(base, LC_MESSAGES="pl_PL", LANG="en_US")), "pl")
            self.assertEqual(L.resolve_language(dict(base, LC_ALL="", LANG="pl")), "pl")
            self.assertEqual(L.resolve_language(dict(base, LANG="de_DE.UTF-8")), "en")
            self.assertEqual(L.resolve_language(base), "en")

    def test_resolve_from_tool_config(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"WARPIPE_CONFIG_DIR": d, "LANG": "en_US.UTF-8"}
            cfg = Path(d) / "config.json"
            self.assertEqual(L.config_path(env), str(cfg))
            cfg.write_text('{"language": "pl"}')
            self.assertEqual(L.resolve_language(env), "pl")
            self.assertEqual(L.resolve_language(dict(env, WARPIPE_LANG="en")), "en")
            cfg.write_text('{"language": "auto"}')
            self.assertEqual(L.resolve_language(dict(env, LANG="pl_PL.UTF-8")), "pl")
            cfg.write_text('{"language": "en"}')
            self.assertEqual(L.resolve_language(dict(env, LANG="pl_PL.UTF-8")), "en")
            cfg.write_text("{zepsuty")
            self.assertEqual(L.resolve_language(dict(env, LANG="pl_PL.UTF-8")), "pl")
        # default location follows XDG_CONFIG_HOME like the tool
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(L.config_path({"XDG_CONFIG_HOME": d}), os.path.join(d, "warpipe", "config.json"))
            self.assertEqual(L.config_path({"HOME": d}), os.path.join(d, ".config", "warpipe", "config.json"))

    def test_config_path_matches_tool(self):
        from warpipe.state import config_dir
        self.assertEqual(L.config_path(), str(config_dir() / "config.json"))

    def test_polish_visuals_and_messages(self):
        self.assertEqual(L.set_language("pl_PL.UTF-8"), "pl")
        v = L.app_visual(WF, st(apps=[("Firefox", True, False, True)]), True)
        self.assertEqual((v.state, v.badge), (S_ABSENT, "brak"))
        self.assertEqual(L.app_visual({"app": "", "label": ""}, st(), True).label, "wybierz aplikację")
        self.assertEqual(L.system_visual(st(False), True).label, "Cały system")
        self.assertIn("teraz nie gra", L.press_for(ACTION_APP, WF, st(apps=[]), True).reason)
        self.assertEqual(L.apps_for_pi(None, False, WF)["lang"], "pl")
        self.assertEqual(L.set_language("auto"), "en")
        self.assertEqual(L.app_visual(WF, None, False).badge, "start")

    def test_polish_catalog_complete(self):
        """Every tr("...") literal and every badge/label msgid has a Polish translation
        with the same {placeholders}; no stale entries."""
        import ast
        import string
        from helpers import PLUGIN_LIB
        used = set()
        for name in ("logic.py", "main.py"):
            with open(os.path.join(PLUGIN_LIB, "warpipe_plugin", name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    used.add(node.args[0].value)
        used |= {b for b in L.BADGE.values() if b} | {L.SYSTEM_LABEL, L.UNSET_LABEL}
        self.assertEqual(sorted(used - set(L.PL)), [])
        self.assertEqual(sorted(set(L.PL) - used), [])

        def fields(s):
            return {f for _, f, _, _ in string.Formatter().parse(s) if f}
        for k, v in L.PL.items():
            self.assertEqual(fields(k), fields(v), k)


class TestResolveTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def exe(self, rel):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return str(p)

    def test_configured_path(self):
        p = self.exe("opt/dd")
        self.assertEqual(L.resolve_tool({"toolPath": p}, {"HOME": str(self.d), "PATH": ""}), ([p, "--tray"], ""))
        argv, why = L.resolve_tool({"toolPath": "~/opt/dd"}, {"HOME": str(self.d), "PATH": ""})
        self.assertEqual(argv, [p, "--tray"])
        argv, why = L.resolve_tool({"toolPath": "opt/dd"}, {"HOME": str(self.d)})
        self.assertIsNone(argv)
        self.assertIn("absolute", why)
        argv, why = L.resolve_tool({"toolPath": str(self.d / "missing")}, {"HOME": str(self.d)})
        self.assertIsNone(argv)
        noexec = self.d / "noexec"
        noexec.write_text("x")
        self.assertIsNone(L.resolve_tool({"toolPath": str(noexec)}, {"HOME": str(self.d)})[0])

    def test_path_then_local_bin(self):
        home = self.d / "home"
        bin1 = self.exe("bin1/warpipe")
        env = {"HOME": str(home), "PATH": f"{self.d / 'nothing'}:{self.d / 'bin1'}"}
        self.assertEqual(L.resolve_tool({}, env)[0], [bin1, "--tray"])
        local = self.exe("home/.local/bin/warpipe")
        self.assertEqual(L.resolve_tool({}, {"HOME": str(home), "PATH": ""})[0], [local, "--tray"])
        os.unlink(local)
        share = self.exe("home/.local/share/warpipe/warpipe")
        self.assertEqual(L.resolve_tool({}, {"HOME": str(home), "PATH": "/usr/bin"})[0], [share, "--tray"])
        os.unlink(share)
        argv, why = L.resolve_tool({}, {"HOME": str(home), "PATH": "/nonexistent"})
        self.assertIsNone(argv)
        self.assertIn("Can't find", why)

    def test_relative_path_entries_ignored(self):
        """"." in PATH = current directory (OpenDeck starts the plugin in its own directory)."""
        self.exe("cwd/warpipe")
        old = os.getcwd()
        os.chdir(self.d / "cwd")
        try:
            argv, _ = L.resolve_tool({}, {"HOME": str(self.d / "home"), "PATH": ".::bin"})
        finally:
            os.chdir(old)
        self.assertIsNone(argv)

    def test_child_env(self):
        env = L.child_env({"PATH": "/usr/bin", "PYTHONPATH": "/evil", "LD_PRELOAD": "x.so", "HOME": "/h",
                           "WAYLAND_DISPLAY": "wayland-0"})
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/h", "WAYLAND_DISPLAY": "wayland-0"})


class TestManifest(unittest.TestCase):
    def setUp(self):
        from helpers import PLUGIN_DIR
        self.dir = PLUGIN_DIR
        with open(os.path.join(PLUGIN_DIR, "manifest.json"), encoding="utf-8") as f:
            self.m = json.load(f)

    def test_shape_like_opendeck_expects(self):
        m = self.m
        for k in ("Name", "Author", "Version", "Icon", "Actions", "OS"):
            self.assertIn(k, m)
        self.assertEqual(m["Name"], "Warpipe")
        self.assertEqual(m["Category"], "Discord")
        self.assertEqual(m["SDKVersion"], 2)
        self.assertEqual(m["OS"], [{"Platform": "linux"}])
        self.assertEqual(m["CodePaths"]["x86_64-unknown-linux-gnu"], "bin/warpipe-plugin")
        self.assertEqual(m["CodePathLin"], "bin/warpipe-plugin")
        exe = os.path.join(self.dir, m["CodePathLin"])
        self.assertTrue(os.stat(exe).st_mode & stat.S_IXUSR)
        with open(exe, encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), "#!/usr/bin/python3 -I")
        uuids = {a["UUID"]: a for a in m["Actions"]}
        self.assertEqual(set(uuids), {ACTION_APP, ACTION_SYSTEM})
        self.assertEqual(uuids[ACTION_APP]["PropertyInspectorPath"], "pi/app.html")
        self.assertNotIn("PropertyInspectorPath", uuids[ACTION_SYSTEM])
        self.assertEqual(uuids[ACTION_APP]["Name"], "App audio")
        self.assertEqual(uuids[ACTION_SYSTEM]["Name"], "Whole system")
        for a in m["Actions"]:
            self.assertEqual(a["Controllers"], ["Keypad"])
            self.assertEqual(len(a["States"]), 1)  # 1 state: OpenDeck does not switch the image by itself
            for icon in [a["Icon"]] + [s["Image"] for s in a["States"]]:
                self.assertTrue(os.path.isfile(os.path.join(self.dir, icon + ".png")), icon)
                self.assertTrue(os.path.isfile(os.path.join(self.dir, icon + "@2x.png")), icon)
        for icon in (m["Icon"], m["CategoryIcon"]):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, icon + ".png")), icon)

    def test_pi_files_offline(self):
        for name in ("app.html", "app.js", "app.css"):
            p = os.path.join(self.dir, "pi", name)
            self.assertTrue(os.path.isfile(p))
            with open(p, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("http://", text.replace("ws://127.0.0.1", ""))
            self.assertNotIn("https://", text)
        with open(os.path.join(self.dir, "pi", "app.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('<script src="app.js"></script>', html)
        self.assertIn('lang="en"', html)


if __name__ == "__main__":
    unittest.main()
