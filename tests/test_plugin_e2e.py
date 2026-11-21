"""OpenDeck plugin as a separate process: fake OpenDeck (WebSocket) + fake tool (socket).

No PipeWire. The plugin is started exactly the way OpenDeck starts it.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import helpers  # noqa: F401
from fake_opendeck import (ACTION_APP, ACTION_SYSTEM, PLUGIN_UUID,
                           FakeControlServer, FakeOpenDeck, context_for,
                           make_status, plugin_env)

try:
    import PySide6.QtWebSockets  # noqa: F401
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

TMP_BASE = os.environ.get("WARPIPE_TEST_TMP", os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
WF = {"app": "Warframe.x64.exe"}
FF = {"app": "Firefox"}


@unittest.skipUnless(HAVE_QT, "no PySide6.QtWebSockets")
class PluginBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_BASE, exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="ddp-e2e.", dir=TMP_BASE)
        self.run_dir = os.path.join(self.tmp, "run")
        self.sock = os.path.join(self.run_dir, "control.sock")
        self.od = FakeOpenDeck()
        self.ctl = None

    def tearDown(self):
        if self.ctl is not None:
            self.ctl.stop()
        rc = self.od.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotIsInstance(rc, tuple, "the plugin did not exit by itself after the connection closed")

    def start_tool(self, status):
        self.ctl = FakeControlServer(self.sock, status).start()
        return self.ctl

    def launch(self, extra_env=None):
        self.od.launch(env=plugin_env(self.run_dir, extra_env), log_path=os.path.join(self.tmp, "plugin.log"))
        uuid = self.od.wait_registered()
        self.assertEqual(uuid, PLUGIN_UUID)
        return uuid


class TestRegistration(PluginBase):
    def test_register_like_opendeck_and_exit_on_close(self):
        self.launch()
        reg = self.od.found(lambda m: m.get("event") == "registerPlugin")[0]
        self.assertEqual(reg, {"event": "registerPlugin", "uuid": "io.github.gawson.warpipe.sdPlugin"})
        g = self.od.wait_for(lambda m: m.get("event") == "getGlobalSettings")
        self.assertEqual(g["context"], PLUGIN_UUID)
        proc = self.od.proc
        self.od.conn.close()
        self.assertEqual(proc.wait(timeout=5), 0)

    def test_garbage_does_not_crash(self):
        self.start_tool(make_status(False, "active", [("Warframe.x64.exe", True, False, True)]))
        self.launch()
        k = context_for(0)
        self.od.conn.send_text("not json")
        self.od.conn.send_text("[1,2,3]")
        self.od.send({"event": "willAppear", "action": "other.plugin.action", "context": context_for(9), "payload": {}})
        self.od.send({"event": "keyDown", "action": ACTION_APP, "context": 12345, "payload": {}})
        self.od.send({"event": "willAppear", "action": ACTION_APP, "context": k, "payload": "bad"})
        self.od.send({"event": "titleParametersDidChange", "action": ACTION_APP, "context": k, "payload": {}})
        self.od.send({"event": "deviceDidConnect", "device": "x", "deviceInfo": {}})
        self.od.will_appear(k, ACTION_APP, {"app": ["list?"], "label": None})
        self.od.wait_image(k, "app:unset")
        self.assertEqual(self.od.images(context_for(9)), [])
        self.assertIsNone(self.od.proc.poll())


class TestVisualsFollowTool(PluginBase):
    def test_states_and_pi(self):
        ctl = self.start_tool(make_status(False, "active", [("Warframe.x64.exe", True, False, True),
                                                           ("Firefox", True, False, False)]))
        self.launch()
        wf, ff, gone, sysk, unset = (context_for(i) for i in range(5))
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.will_appear(ff, ACTION_APP, FF)
        self.od.will_appear(gone, ACTION_APP, {"app": "Spotify", "label": "Muzyka"})
        self.od.will_appear(sysk, ACTION_SYSTEM, {})
        self.od.will_appear(unset, ACTION_APP, {})
        self.od.wait_image(wf, "app:on")
        self.od.wait_image(ff, "app:off")
        img = self.od.wait_image(gone, "app:absent")
        self.assertEqual((img["label"], img["badge"]), ("Muzyka", "not playing"))
        self.od.wait_image(sysk, "system:sys_on")
        self.od.wait_image(unset, "app:unset")
        # the first image is already the right state — no "tool not running" flash
        self.assertEqual(self.od.images(wf)[0]["state"], "app:on")

        # tool state changes (e.g. after a press elsewhere)
        ctl.set_status(make_status(True, "active", [("Warframe.x64.exe", True, False, False),
                                                    ("Firefox", True, True, True)]))
        self.od.wait_image(wf, "app:off")
        self.od.wait_image(ff, "app:on")
        self.od.wait_image(sysk, "system:sys_off")
        # no stream: neutral, with a hint; selected -> ring
        ctl.set_status(make_status(True, "none", [("Warframe.x64.exe", True, False, False),
                                                  ("Firefox", True, True, False)]))
        self.od.wait_image(ff, "app:nostream", ring=True)
        self.od.wait_image(wf, "app:nostream", ring=False)
        # the image is sent only when it changes
        n = len(self.od.images(wf))
        ctl.set_status(make_status(True, "none", [("Warframe.x64.exe", True, False, False),
                                                  ("Firefox", True, True, False)]))
        time.sleep(0.4)
        self.assertEqual(len(self.od.images(wf)), n)

        # property inspector: app list
        m0 = self.od.mark()
        self.od.send_to_plugin(wf, ACTION_APP, {"cmd": "listApps"})
        pi = self.od.wait_for(lambda m: m.get("event") == "sendToPropertyInspector", since=m0)
        self.assertEqual((pi["context"], pi["action"]), (wf, ACTION_APP))
        p = pi["payload"]
        self.assertEqual(p["type"], "apps")
        self.assertTrue(p["toolRunning"])
        self.assertEqual(p["capture"], "none")
        self.assertEqual({a["key"] for a in p["apps"]}, {"Warframe.x64.exe", "Firefox"})
        self.assertEqual(p["current"], {"app": "Warframe.x64.exe", "label": ""})
        self.assertEqual(p["lang"], "en")
        # a visible inspector gets updates by itself
        self.od.pi_did_appear(wf, ACTION_APP)
        m1 = self.od.mark()
        ctl.set_status(make_status(True, "active", [("Firefox", True, True, True)]))
        self.od.wait_for(lambda m: m.get("event") == "sendToPropertyInspector" and m["context"] == wf
                         and [a["key"] for a in m["payload"]["apps"]] == ["Firefox"], since=m1)
        self.od.wait_image(wf, "app:absent")
        # the inspector saves a new app -> the key changes right away
        self.od.did_receive_settings(wf, ACTION_APP, {"app": "firefox", "label": ""})
        self.od.wait_image(wf, "app:on")
        # the system key sends nothing to an inspector (it has none)
        self.od.send_to_plugin(sysk, ACTION_SYSTEM, {"cmd": "listApps"})
        time.sleep(0.3)
        self.assertEqual(self.od.found(lambda m: m.get("event") == "sendToPropertyInspector"
                                       and m.get("context") == sysk), [])

    def test_tool_goes_away_and_comes_back(self):
        status = make_status(False, "active", [("Warframe.x64.exe", True, False, True)])
        self.start_tool(status)
        self.launch()
        wf = context_for(0)
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.wait_image(wf, "app:on")
        self.ctl.stop()
        self.od.wait_image(wf, "app:down")
        self.ctl = FakeControlServer(self.sock, status).start()
        self.od.wait_image(wf, "app:on", timeout=6)

    def test_polish_when_asked(self):
        """WARPIPE_LANG=pl (same switch as the tool): Polish badges and "lang" for the inspector."""
        self.start_tool(make_status(False, "active", [("Firefox", True, False, True)]))
        self.launch(extra_env={"WARPIPE_LANG": "pl"})
        wf, sysk = context_for(0), context_for(1)
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.will_appear(sysk, ACTION_SYSTEM, {})
        img = self.od.wait_image(wf, "app:absent")
        self.assertEqual(img["badge"], "brak")
        img = self.od.wait_image(sysk, "system:sys_on")
        self.assertEqual((img["label"], img["badge"]), ("Cały system", "włączony"))
        m0 = self.od.mark()
        self.od.send_to_plugin(wf, ACTION_APP, {"cmd": "listApps"})
        pi = self.od.wait_for(lambda m: m.get("event") == "sendToPropertyInspector", since=m0)
        self.assertEqual(pi["payload"]["lang"], "pl")

    def test_pipewire_error_shown(self):
        self.start_tool(make_status(False, "none", [], error="Can't read the PipeWire state"))
        self.launch()
        self.od.will_appear(context_for(0), ACTION_APP, WF)
        img = self.od.wait_image(context_for(0), "app:error")
        self.assertEqual(img["badge"], "error")


class TestPresses(PluginBase):
    def test_toggle_system_and_alerts(self):
        ctl = self.start_tool(make_status(False, "active", [("Warframe.x64.exe", True, False, True)]))
        self.launch()
        wf, gone, sysk, unset = (context_for(i) for i in range(4))
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.will_appear(gone, ACTION_APP, {"app": "Spotify"})
        self.od.will_appear(sysk, ACTION_SYSTEM, {})
        self.od.will_appear(unset, ACTION_APP, {})
        self.od.wait_image(wf, "app:on")
        self.od.key_down(wf, ACTION_APP, WF)
        cmd = ctl.wait_command(lambda c: c.get("cmd") == "toggle")
        self.assertEqual(cmd["app"], "Warframe.x64.exe")
        self.assertTrue(str(cmd["id"]).startswith("p"))
        self.od.key_up(wf, ACTION_APP, WF)   # keyUp nic nie robi
        self.od.key_down(sysk, ACTION_SYSTEM, {})
        ctl.wait_command(lambda c: c.get("cmd") == "system")
        # an app that is not playing (and not selected) -> showAlert, no command
        m0 = self.od.mark()
        self.od.key_down(gone, ACTION_APP, {"app": "Spotify"})
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == gone, since=m0)
        log = self.od.wait_for(lambda m: m.get("event") == "logMessage" and "Spotify" in m["payload"]["message"],
                               since=m0)
        self.assertIn("is not playing right now", log["payload"]["message"])
        self.od.key_down(unset, ACTION_APP, {})
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == unset, since=m0)
        time.sleep(0.3)
        self.assertEqual([c["cmd"] for c in ctl.commands], ["subscribe", "toggle", "system"])
        # the tool answers with an error -> showAlert on that key
        ctl.reply_ok = False
        m1 = self.od.mark()
        self.od.key_down(wf, ACTION_APP, WF)
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == wf, since=m1)


class TestLaunchTool(PluginBase):
    def make_launcher(self, body):
        path = os.path.join(self.tmp, "fake launcher")    # a space in the path: no shell in between
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_launch_detached_with_clean_env(self):
        marker = os.path.join(self.tmp, "launched.txt")
        launcher = self.make_launcher(
            f'{{ echo "args=$*"; echo "cwd=$(pwd)"; env; }} > "{marker}"\n')
        self.launch(extra_env={"PYTHONPATH": "/zle/miejsce", "LD_PRELOAD": ""})
        wf, sysk = context_for(0), context_for(1)
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.will_appear(sysk, ACTION_SYSTEM, {})
        img = self.od.wait_image(wf, "app:down")
        self.assertEqual(img["badge"], "start")
        self.od.wait_image(sysk, "system:down")
        self.od.did_receive_global_settings({"toolPath": launcher})
        time.sleep(0.3)
        self.od.key_down(wf, ACTION_APP, WF)
        self.od.wait_image(wf, "app:starting")
        self.od.wait_image(sysk, "system:starting")
        end = time.monotonic() + 5
        while not (os.path.exists(marker) and os.path.getsize(marker)) and time.monotonic() < end:
            time.sleep(0.05)
        with open(marker) as f:
            text = f.read()
        self.assertIn("args=--tray", text)
        self.assertIn(f"cwd={os.path.expanduser('~')}", text)
        self.assertNotIn("PYTHONPATH=", text)
        self.assertNotIn("LD_PRELOAD=", text)
        # a second press while starting does not start it again
        os.unlink(marker)
        self.od.key_down(wf, ACTION_APP, WF)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(marker))
        # the tool is up -> normal appearance
        self.start_tool(make_status(False, "active", [("Warframe.x64.exe", True, False, True)]))
        self.od.wait_image(wf, "app:on", timeout=5)
        self.od.wait_image(sysk, "system:sys_on")

    def test_plugin_does_not_leak_offscreen_to_tool(self):
        """The plugin sets offscreen only for itself (Qt argv), not in the tray icon's environment."""
        marker = os.path.join(self.tmp, "env.txt")
        launcher = self.make_launcher(f'env > "{marker}"\n')
        env = plugin_env(self.run_dir)
        env.pop("QT_QPA_PLATFORM", None)
        self.od.launch(env=env, log_path=os.path.join(self.tmp, "plugin.log"))
        self.od.wait_registered()
        self.od.will_appear(context_for(0), ACTION_SYSTEM, {})
        self.od.wait_image(context_for(0), "system:down")
        self.od.did_receive_global_settings({"toolPath": launcher})
        time.sleep(0.3)
        self.od.key_down(context_for(0), ACTION_SYSTEM, {})
        end = time.monotonic() + 5
        while not (os.path.exists(marker) and os.path.getsize(marker)) and time.monotonic() < end:
            time.sleep(0.05)
        with open(marker) as f:
            self.assertNotIn("QT_QPA_PLATFORM", f.read())

    def test_bad_tool_path_alerts(self):
        self.launch(extra_env={"PATH": "/usr/bin:/bin", "HOME": os.path.join(self.tmp, "home")})
        wf = context_for(0)
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.wait_image(wf, "app:down")
        self.od.did_receive_global_settings({"toolPath": "relative/path"})
        time.sleep(0.3)
        m0 = self.od.mark()
        self.od.key_down(wf, ACTION_APP, WF)
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == wf, since=m0)
        log = self.od.wait_for(lambda m: m.get("event") == "logMessage", since=m0)
        self.assertIn("absolute", log["payload"]["message"])
        self.od.wait_image(wf, "app:down")


class TestKeyPressedWhileToolDown(PluginBase):
    """Review findings: a key pressed while the tool is down must keep its meaning
    ("Whole system" must not end up applying the saved selection), and a tool that
    exits right away must be reported at once instead of "starting…" for 20 s."""

    def make_launcher(self, body):
        path = os.path.join(self.tmp, "launcher")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def setup_keys(self, launcher):
        self.launch()
        wf, ff, sysk = context_for(0), context_for(1), context_for(2)
        self.od.will_appear(wf, ACTION_APP, WF)
        self.od.will_appear(ff, ACTION_APP, FF)
        self.od.will_appear(sysk, ACTION_SYSTEM, {})
        self.od.wait_image(sysk, "system:down")
        self.od.did_receive_global_settings({"toolPath": launcher})
        time.sleep(0.3)
        return wf, ff, sysk

    # the tool starts with its SAVED selection: only Warframe
    SAVED = make_status(True, "active", [("Warframe.x64.exe", True, True, True), ("Firefox", True, False, False)])

    def test_whole_system_pressed_while_down(self):
        marker = os.path.join(self.tmp, "launched")
        wf, ff, sysk = self.setup_keys(self.make_launcher(f'echo "$*" >> "{marker}"\n'))
        self.od.key_down(sysk, ACTION_SYSTEM, {})
        self.od.wait_image(sysk, "system:starting")
        ctl = self.start_tool(self.SAVED)
        cmd = ctl.wait_command(lambda c: c.get("cmd") == "system")
        self.assertTrue(str(cmd["id"]).startswith("p"))
        time.sleep(0.3)
        self.assertEqual([c["cmd"] for c in ctl.commands], ["subscribe", "system"])
        with open(marker) as f:
            self.assertEqual(f.read().split(), ["--tray"])    # started once

    def test_app_key_pressed_while_down_last_press_wins(self):
        wf, ff, sysk = self.setup_keys(self.make_launcher("exit 0\n"))
        self.od.key_down(sysk, ACTION_SYSTEM, {})
        self.od.wait_image(ff, "app:starting")
        m0 = self.od.mark()
        self.od.key_down(ff, ACTION_APP, FF)         # pressed while starting: replaces "system"
        log = self.od.wait_for(lambda m: m.get("event") == "logMessage" and "starting" in m["payload"]["message"]
                               and "applied" in m["payload"]["message"], since=m0)
        self.assertIn("Warpipe is starting", log["payload"]["message"])
        ctl = self.start_tool(self.SAVED)
        cmd = ctl.wait_command(lambda c: c.get("cmd") == "set")
        self.assertEqual({k: cmd[k] for k in ("app", "on", "require_present")},
                         {"app": "Firefox", "on": True, "require_present": True})
        time.sleep(0.3)
        self.assertEqual([c["cmd"] for c in ctl.commands], ["subscribe", "set"])
        # the tool refusing it (e.g. the app is not playing) -> alert on that key
        ctl.reply_ok = False
        m1 = self.od.mark()
        self.od.key_down(ff, ACTION_APP, FF)
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == ff, since=m1)

    def test_tool_exits_right_away(self):
        counter = os.path.join(self.tmp, "starts")
        wf, ff, sysk = self.setup_keys(self.make_launcher(f'echo x >> "{counter}"\nexit 3\n'))
        m0 = self.od.mark()
        t0 = time.monotonic()
        self.od.key_down(wf, ACTION_APP, WF)
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == wf, 5, since=m0)
        log = self.od.wait_for(lambda m: m.get("event") == "logMessage" and "exit code 3" in m["payload"]["message"],
                               5, since=m0)
        self.assertIn("exited right away", log["payload"]["message"])
        self.od.wait_image(wf, "app:down", timeout=5)
        self.od.wait_image(sysk, "system:down", timeout=5)
        self.assertLess(time.monotonic() - t0, 10)   # not STARTING_S (20 s)
        self.assertEqual([i["state"] for i in self.od.images(wf, since=m0)], ["app:starting", "app:down"])
        # the next press is not ignored: it tries again right away
        self.od.key_down(wf, ACTION_APP, WF)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            with open(counter) as f:
                if len(f.read().split()) >= 2:
                    break
            time.sleep(0.05)
        with open(counter) as f:
            self.assertEqual(len(f.read().split()), 2)


class TestPropertyInspectorScript(unittest.TestCase):
    """pi/app.js in node (if available): syntax + pure functions (list, status line)."""

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_pi_functions(self):
        from helpers import PLUGIN_DIR
        js = os.path.join(PLUGIN_DIR, "pi", "app.js")
        subprocess.run(["node", "--check", js], check=True, timeout=20)
        script = r"""
const P = require(process.argv[1]);
const apps = [
  {key: "Warframe.x64.exe", display: "Warframe.x64.exe", present: true, in_stream: true, selected: true},
  {key: "Firefox", display: "Firefox", present: true, in_stream: false, selected: false},
  {key: "Spotify", display: "Spotify", present: false, in_stream: false, selected: true},
];
const out = {
  opts: P.buildOptions(apps, "warframe.x64.exe", true),
  optsMissing: P.buildOptions(apps, "Minecraft", true),
  optsDown: P.buildOptions([], "", false),
  optsNone: P.buildOptions([], "", true),
  st1: P.statusLine(true, "active", apps, "Warframe.x64.exe"),
  st2: P.statusLine(true, "none", apps, "Spotify"),
  st3: P.statusLine(false, "", [], "Warframe.x64.exe"),
  clean: P.clean("  a\u0000b\nc  ", 200),
};
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", script, js], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        texts = [o["text"] for o in out["opts"]]
        self.assertEqual(texts, ["— pick from the list —", "Warframe.x64.exe — on stream", "Firefox"])
        self.assertEqual([o["selected"] for o in out["opts"]], [False, True, False])
        self.assertEqual(out["optsMissing"][-1], {"value": "Minecraft", "text": "Minecraft (not playing now)",
                                                  "selected": True})
        self.assertEqual(out["optsDown"][0]["text"], "— list unavailable —")
        self.assertEqual(out["optsNone"][0]["text"], "— no app is playing right now —")
        self.assertEqual(out["st1"]["cls"], "ok")
        self.assertIn("Warframe.x64.exe: on stream.", out["st1"]["text"])
        self.assertIn("not detected", out["st2"]["text"])
        self.assertIn("Spotify: not playing now.", out["st2"]["text"])
        self.assertEqual(out["st3"]["cls"], "bad")
        self.assertIn("is not running", out["st3"]["text"])
        self.assertEqual(out["clean"], "abc")

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_pi_polish(self):
        """setLang("pl") / payload.lang: Polish options, status line and static texts."""
        from helpers import PLUGIN_DIR
        js = os.path.join(PLUGIN_DIR, "pi", "app.js")
        script = r"""
const fs = require("fs"), vm = require("vm");
const P = require(process.argv[1]);
const apps = [{key: "Firefox", display: "Firefox", present: true, in_stream: true}];
P.setLang("pl_PL.UTF-8");
const pl = {opts: P.buildOptions(apps, "Spotify", true), none: P.buildOptions([], "", true),
            st: P.statusLine(true, "none", apps, "Firefox"), down: P.statusLine(false, "", [], "")};
const back = P.setLang("auto");
const en = P.buildOptions([], "", false)[0].text;
// same keys in both tables
const sameKeys = JSON.stringify(Object.keys(P.T.en).sort()) === JSON.stringify(Object.keys(P.T.pl).sort());
// a page in a vm: static texts follow "lang" from the plugin
const els = {};
function el(id) {
  return els[id] || (els[id] = {id, value: "", textContent: "", className: "", children: [], listeners: {},
    firstChild: null, attrs: {},
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); this.firstChild = this.children[0]; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); this.firstChild = this.children[0] || null; },
    getAttribute(a) { return this.attrs[a]; }, setAttribute(a, v) { this.attrs[a] = v; },
  });
}
const adv = el("adv"); adv.attrs["data-i18n"] = "advanced"; adv.textContent = "Advanced";
const key = el("app-key"); key.attrs["data-i18n-placeholder"] = "keyPlaceholder";
const ref = el("refresh"); ref.attrs["data-i18n-title"] = "refresh";
const root = {lang: "en"};
const doc = { getElementById: el, createElement: () => ({}), activeElement: null, documentElement: root,
  querySelectorAll(sel) {
    const attr = sel.slice(1, -1);
    return Object.values(els).filter(e => e.attrs[attr] !== undefined);
  } };
class FakeWS { constructor(url) { this.readyState = 1; FakeWS.last = this; } send(t) {} }
const ctx = { document: doc, WebSocket: FakeWS, setTimeout, clearTimeout, JSON, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
ctx.connectOpenActionSocket(1, "CTX", "registerPropertyInspector",
  JSON.stringify({application: {language: "en"}}), "{}");
const before = {adv: adv.textContent, status: els["status"].textContent, lang: root.lang};
FakeWS.last.onmessage({data: JSON.stringify({event: "sendToPropertyInspector", context: "CTX",
  payload: {type: "apps", lang: "pl", toolRunning: true, capture: "active", apps: []}})});
const after = {adv: adv.textContent, status: els["status"].textContent, lang: root.lang,
  placeholder: key.attrs.placeholder, title: ref.attrs.title};
console.log(JSON.stringify({pl, back, en, sameKeys, before, after}));
"""
        r = subprocess.run(["node", "-e", script, js], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        pl = out["pl"]
        self.assertEqual([o["text"] for o in pl["opts"]],
                         ["— wybierz z listy —", "Firefox — na streamie", "Spotify (teraz nie gra)"])
        self.assertEqual(pl["none"][0]["text"], "— żadna aplikacja teraz nie gra —")
        self.assertIn("Stream z dźwiękiem: nie wykryty", pl["st"]["text"])
        self.assertIn("Firefox: na streamie.", pl["st"]["text"])
        self.assertIn("nie działa", pl["down"]["text"])
        self.assertEqual(out["back"], "en")
        self.assertEqual(out["en"], "— list unavailable —")
        self.assertTrue(out["sameKeys"])
        self.assertEqual(out["before"], {"adv": "Advanced", "status": "Connecting to the plugin…", "lang": "en"})
        self.assertEqual(out["after"], {"adv": "Zaawansowane", "status": "Stream z dźwiękiem: wykryty. "
                                        "Wybierz aplikację z listy.", "lang": "pl",
                                        "placeholder": "np. Warframe.x64.exe (gdy teraz nie gra)",
                                        "title": "Odśwież listę"})

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_pi_connect_registers_and_saves(self):
        """Like OpenDeck: after loading the page call connectOpenActionSocket(...) as a plain
        script (vm, no require) with a fake WebSocket and DOM; check the messages sent."""
        from helpers import PLUGIN_DIR
        js = os.path.join(PLUGIN_DIR, "pi", "app.js")
        script = r"""
const fs = require("fs"), vm = require("vm");
const els = {};
function el(id) {
  return els[id] || (els[id] = {id, value: "", textContent: "", className: "", children: [], listeners: {},
    firstChild: null,
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); this.firstChild = this.children[0]; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); this.firstChild = this.children[0] || null; },
  });
}
const sent = [];
class FakeWS { constructor(url) { this.url = url; this.readyState = 1; FakeWS.last = this; }
  send(t) { sent.push(JSON.parse(t)); } }
const ctx = { document: { getElementById: el, createElement: () => ({}), activeElement: null },
              WebSocket: FakeWS, setTimeout, clearTimeout, JSON, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const both = typeof ctx.connectElgatoStreamDeckSocket + "/" + typeof ctx.connectOpenActionSocket;
const actionInfo = JSON.stringify({action: "io.github.gawson.warpipe.app", context: "CTX",
  payload: {settings: {app: "Warframe.x64.exe", label: ""}}});
ctx.connectOpenActionSocket(57116, "CTX", "registerPropertyInspector", "{}", actionInfo);
ctx.connectElgatoStreamDeckSocket(57116, "CTX", "registerPropertyInspector", "{}", actionInfo);  // second call: no-op
FakeWS.last.onopen();
FakeWS.last.onmessage({data: JSON.stringify({event: "sendToPropertyInspector", action: "a", context: "CTX",
  payload: {type: "apps", toolRunning: true, capture: "active", enforce: false,
            apps: [{key: "Firefox", display: "Firefox", present: true, in_stream: true}, {key: 5}]}})});
const options = els["app-select"].children.map(o => o.textContent);
els["app-select"].listeners.change[0]({target: {value: "Firefox"}});
els["tool-path"].value = " /opt/dd ";
els["tool-path"].listeners.change[0]();
console.log(JSON.stringify({both, url: FakeWS.last.url, sent, options,
  status: els["status"].textContent, key: els["app-key"].value}));
"""
        r = subprocess.run(["node", "-e", script, js], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["both"], "function/function")
        self.assertEqual(out["url"], "ws://127.0.0.1:57116")
        sent = out["sent"]
        self.assertEqual(sent[0], {"event": "registerPropertyInspector", "uuid": "CTX"})
        self.assertEqual(sent[1], {"event": "getGlobalSettings", "context": "CTX"})
        self.assertEqual(sent[2], {"action": "io.github.gawson.warpipe.app", "event": "sendToPlugin",
                                   "context": "CTX", "payload": {"cmd": "listApps"}})
        self.assertEqual(sent[3], {"event": "setSettings", "context": "CTX",
                                   "payload": {"app": "Firefox", "label": ""}})
        self.assertEqual(sent[4], {"event": "setGlobalSettings", "context": "CTX",
                                   "payload": {"toolPath": "/opt/dd"}})
        self.assertEqual(len(sent), 5)
        self.assertEqual(out["options"], ["— pick from the list —", "Firefox — on stream",
                                          "Warframe.x64.exe (not playing now)"])
        self.assertEqual(out["key"], "Firefox")
        self.assertIn("Firefox: on stream", out["status"])


if __name__ == "__main__":
    unittest.main()
