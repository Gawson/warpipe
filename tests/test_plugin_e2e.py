"""Wtyczka OpenDeck jako osobny proces: atrapa OpenDeck (WebSocket) + atrapa narzędzia (gniazdo).

Bez PipeWire. Wtyczka jest uruchamiana dokładnie tak, jak robi to OpenDeck.
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


@unittest.skipUnless(HAVE_QT, "brak PySide6.QtWebSockets")
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
        self.assertNotIsInstance(rc, tuple, "wtyczka nie zakończyła się sama po zamknięciu połączenia")

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
        self.od.conn.send_text("to nie json")
        self.od.conn.send_text("[1,2,3]")
        self.od.send({"event": "willAppear", "action": "inna.wtyczka.akcja", "context": context_for(9), "payload": {}})
        self.od.send({"event": "keyDown", "action": ACTION_APP, "context": 12345, "payload": {}})
        self.od.send({"event": "willAppear", "action": ACTION_APP, "context": k, "payload": "zły"})
        self.od.send({"event": "titleParametersDidChange", "action": ACTION_APP, "context": k, "payload": {}})
        self.od.send({"event": "deviceDidConnect", "device": "x", "deviceInfo": {}})
        self.od.will_appear(k, ACTION_APP, {"app": ["lista?"], "label": None})
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
        self.assertEqual((img["label"], img["badge"]), ("Muzyka", "brak"))
        self.od.wait_image(sysk, "system:sys_on")
        self.od.wait_image(unset, "app:unset")
        # pierwszy obrazek to już właściwy stan — bez mignięcia „narzędzie nie działa”
        self.assertEqual(self.od.images(wf)[0]["state"], "app:on")

        # zmiana stanu narzędzia (np. po naciśnięciu w innym miejscu)
        ctl.set_status(make_status(True, "active", [("Warframe.x64.exe", True, False, False),
                                                    ("Firefox", True, True, True)]))
        self.od.wait_image(wf, "app:off")
        self.od.wait_image(ff, "app:on")
        self.od.wait_image(sysk, "system:sys_off")
        # brak streamu: neutralnie, z podpowiedzią; zaznaczona → ramka
        ctl.set_status(make_status(True, "none", [("Warframe.x64.exe", True, False, False),
                                                  ("Firefox", True, True, False)]))
        self.od.wait_image(ff, "app:nostream", ring=True)
        self.od.wait_image(wf, "app:nostream", ring=False)
        # obrazek wysyłany tylko przy zmianie
        n = len(self.od.images(wf))
        ctl.set_status(make_status(True, "none", [("Warframe.x64.exe", True, False, False),
                                                  ("Firefox", True, True, False)]))
        time.sleep(0.4)
        self.assertEqual(len(self.od.images(wf)), n)

        # inspektor właściwości: lista aplikacji
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
        # widoczny inspektor dostaje aktualizacje sam
        self.od.pi_did_appear(wf, ACTION_APP)
        m1 = self.od.mark()
        ctl.set_status(make_status(True, "active", [("Firefox", True, True, True)]))
        self.od.wait_for(lambda m: m.get("event") == "sendToPropertyInspector" and m["context"] == wf
                         and [a["key"] for a in m["payload"]["apps"]] == ["Firefox"], since=m1)
        self.od.wait_image(wf, "app:absent")
        # inspektor zapisuje nową aplikację → przycisk od razu się zmienia
        self.od.did_receive_settings(wf, ACTION_APP, {"app": "firefox", "label": ""})
        self.od.wait_image(wf, "app:on")
        # system key niczego nie wysyła do inspektora (nie ma go)
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

    def test_pipewire_error_shown(self):
        self.start_tool(make_status(False, "none", [], error="Nie mogę odczytać stanu PipeWire"))
        self.launch()
        self.od.will_appear(context_for(0), ACTION_APP, WF)
        img = self.od.wait_image(context_for(0), "app:error")
        self.assertEqual(img["badge"], "błąd")


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
        # aplikacja, która nie gra (i nie jest zaznaczona) → showAlert, bez polecenia
        m0 = self.od.mark()
        self.od.key_down(gone, ACTION_APP, {"app": "Spotify"})
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == gone, since=m0)
        log = self.od.wait_for(lambda m: m.get("event") == "logMessage" and "Spotify" in m["payload"]["message"],
                               since=m0)
        self.assertIn("teraz nie gra", log["payload"]["message"])
        self.od.key_down(unset, ACTION_APP, {})
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == unset, since=m0)
        time.sleep(0.3)
        self.assertEqual([c["cmd"] for c in ctl.commands], ["subscribe", "toggle", "system"])
        # narzędzie odpowiada błędem → showAlert na tym przycisku
        ctl.reply_ok = False
        m1 = self.od.mark()
        self.od.key_down(wf, ACTION_APP, WF)
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == wf, since=m1)


class TestLaunchTool(PluginBase):
    def make_launcher(self, body):
        path = os.path.join(self.tmp, "fake launcher")    # spacja w ścieżce: żadnej powłoki po drodze
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
        self.assertEqual(img["badge"], "uruchom")
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
        # drugie naciśnięcie w trakcie uruchamiania nie uruchamia drugi raz
        os.unlink(marker)
        self.od.key_down(wf, ACTION_APP, WF)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(marker))
        # narzędzie wstało → zwykły wygląd
        self.start_tool(make_status(False, "active", [("Warframe.x64.exe", True, False, True)]))
        self.od.wait_image(wf, "app:on", timeout=5)
        self.od.wait_image(sysk, "system:sys_on")

    def test_plugin_does_not_leak_offscreen_to_tool(self):
        """Wtyczka ustawia offscreen tylko sobie (argv Qt), nie w środowisku dla ikony narzędzia."""
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
        self.od.did_receive_global_settings({"toolPath": "względna/ścieżka"})
        time.sleep(0.3)
        m0 = self.od.mark()
        self.od.key_down(wf, ACTION_APP, WF)
        self.od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == wf, since=m0)
        log = self.od.wait_for(lambda m: m.get("event") == "logMessage", since=m0)
        self.assertIn("pełna", log["payload"]["message"])
        self.od.wait_image(wf, "app:down")


class TestPropertyInspectorScript(unittest.TestCase):
    """pi/app.js w node (jeśli jest): składnia + czyste funkcje (lista, opis stanu)."""

    @unittest.skipUnless(shutil.which("node"), "brak node")
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
        self.assertEqual(texts, ["— wybierz z listy —", "Warframe.x64.exe — na streamie", "Firefox"])
        self.assertEqual([o["selected"] for o in out["opts"]], [False, True, False])
        self.assertEqual(out["optsMissing"][-1], {"value": "Minecraft", "text": "Minecraft (teraz nie gra)",
                                                  "selected": True})
        self.assertEqual(out["optsDown"][0]["text"], "— lista niedostępna —")
        self.assertEqual(out["optsNone"][0]["text"], "— żadna aplikacja teraz nie gra —")
        self.assertEqual(out["st1"]["cls"], "ok")
        self.assertIn("na streamie", out["st1"]["text"])
        self.assertIn("nie wykryty", out["st2"]["text"])
        self.assertIn("teraz nie gra", out["st2"]["text"])
        self.assertEqual(out["st3"]["cls"], "bad")
        self.assertIn("nie działa", out["st3"]["text"])
        self.assertEqual(out["clean"], "abc")

    @unittest.skipUnless(shutil.which("node"), "brak node")
    def test_pi_connect_registers_and_saves(self):
        """Jak OpenDeck: po załadowaniu strony wołamy connectOpenActionSocket(...) jako zwykły
        skrypt (vm, bez require) z atrapą WebSocket i DOM; sprawdzamy wysyłane wiadomości."""
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
ctx.connectElgatoStreamDeckSocket(57116, "CTX", "registerPropertyInspector", "{}", actionInfo);  // drugi raz: nic
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
        self.assertEqual(out["options"], ["— wybierz z listy —", "Firefox — na streamie",
                                          "Warframe.x64.exe (teraz nie gra)"])
        self.assertEqual(out["key"], "Firefox")
        self.assertIn("Firefox: na streamie", out["status"])


if __name__ == "__main__":
    unittest.main()
