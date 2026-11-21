/* Warpipe — ustawienia przycisku „Dźwięk z aplikacji” (inspektor właściwości).
 *
 * OpenDeck po załadowaniu tej strony woła connectOpenActionSocket(...) (jeśli istnieje)
 * albo connectElgatoStreamDeckSocket(...); oprogramowanie Elgato woła
 * connectElgatoStreamDeckSocket(...). Argumenty są te same:
 *   (port, uuid tego inspektora, "registerPropertyInspector", info JSON, actionInfo JSON)
 *
 * Bez zewnętrznych bibliotek i bez internetu.
 */
"use strict";

var DDPI = (function () {
  var S = {
    ws: null,
    uuid: null,
    action: null,
    settings: { app: "", label: "" },
    global: { toolPath: "" },
    apps: [],
    toolRunning: null,
    capture: "",
    enforce: false
  };
  var timers = {};

  // ------------------------------------------------------------ czyste funkcje
  function clean(v, limit) {
    if (typeof v !== "string") return "";
    var out = "";
    for (var i = 0; i < v.length; i++) {
      var c = v.charCodeAt(i);
      if (c >= 32 && c !== 127) out += v.charAt(i);
    }
    return out.trim().slice(0, limit || 200).trim();
  }

  function sameKey(a, b) {
    return String(a || "").toLowerCase() === String(b || "").toLowerCase();
  }

  function findApp(apps, key) {
    for (var i = 0; i < apps.length; i++) if (sameKey(apps[i].key, key)) return apps[i];
    return null;
  }

  // Lista opcji listy rozwijanej: [{value, text, selected}]
  function buildOptions(apps, current, toolRunning) {
    var present = apps.filter(function (a) { return a.present; });
    var first = present.length ? "— wybierz z listy —"
      : (toolRunning ? "— żadna aplikacja teraz nie gra —" : "— lista niedostępna —");
    var opts = [{ value: "", text: first, selected: !current }];
    present.forEach(function (a) {
      opts.push({
        value: a.key,
        text: (a.display || a.key) + (a.in_stream ? " — na streamie" : ""),
        selected: !!current && sameKey(a.key, current)
      });
    });
    if (current && !findApp(present, current)) {
      opts.push({ value: current, text: current + " (teraz nie gra)", selected: true });
    }
    return opts;
  }

  function statusLine(toolRunning, capture, apps, current) {
    if (toolRunning === null) return { text: "Łączę się z wtyczką…", cls: "" };
    if (!toolRunning) {
      return { text: "Program „Warpipe” nie działa. Naciśnij przycisk na Stream Decku, " +
                     "żeby go uruchomić (albo uruchom go z menu programów).", cls: "bad" };
    }
    var t, cls;
    if (capture === "active") { t = "Stream z dźwiękiem: wykryty."; cls = "ok"; }
    else if (capture === "ambiguous") { t = "Stream z dźwiękiem: niepewny — wybierz go w menu ikony programu."; cls = "warn"; }
    else { t = "Stream z dźwiękiem: nie wykryty. Wybór zadziała sam, gdy zaczniesz stream z dźwiękiem."; cls = "warn"; }
    if (current) {
      var a = findApp(apps, current);
      if (!a || !a.present) t += " " + current + ": teraz nie gra.";
      else if (a.in_stream) t += " " + current + ": na streamie.";
      else t += " " + current + ": gra, ale nie idzie do streamu.";
    } else {
      t += " Wybierz aplikację z listy.";
    }
    return { text: t, cls: cls };
  }

  // ------------------------------------------------------------ komunikacja
  function send(obj) {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
  }

  function saveSettings() {
    send({ event: "setSettings", context: S.uuid, payload: { app: S.settings.app, label: S.settings.label } });
  }

  function saveGlobal() {
    send({ event: "setGlobalSettings", context: S.uuid, payload: { toolPath: S.global.toolPath } });
  }

  function requestApps() {
    send({ action: S.action, event: "sendToPlugin", context: S.uuid, payload: { cmd: "listApps" } });
  }

  function later(name, ms, fn) {
    if (timers[name]) clearTimeout(timers[name]);
    timers[name] = setTimeout(fn, ms);
  }

  // ------------------------------------------------------------ DOM
  function $(id) { return document.getElementById(id); }

  function render() {
    var sel = $("app-select");
    if (!sel) return;
    var opts = buildOptions(S.apps, S.settings.app, S.toolRunning);
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    opts.forEach(function (o) {
      var el = document.createElement("option");
      el.value = o.value;
      el.textContent = o.text;   // textContent: nazwy aplikacji nigdy jako HTML
      if (o.selected) el.selected = true;
      sel.appendChild(el);
    });
    var st = statusLine(S.toolRunning, S.capture, S.apps, S.settings.app);
    var p = $("status");
    p.textContent = st.text;
    p.className = "hint" + (st.cls ? " " + st.cls : "");
  }

  function fillForm() {
    var k = $("app-key"), l = $("label"), t = $("tool-path");
    if (k && document.activeElement !== k) k.value = S.settings.app || "";
    if (l && document.activeElement !== l) l.value = S.settings.label || "";
    if (t && document.activeElement !== t) t.value = S.global.toolPath || "";
    render();
  }

  function bind() {
    $("app-select").addEventListener("change", function (e) {
      var v = clean(e.target.value, 200);
      if (!v) return;
      S.settings.app = v;
      $("app-key").value = v;
      saveSettings();
      render();
    });
    var key = $("app-key");
    var commitKey = function () {
      var v = clean(key.value, 200);
      if (v === S.settings.app) return;
      S.settings.app = v;
      saveSettings();
      render();
    };
    key.addEventListener("change", commitKey);
    key.addEventListener("input", function () { later("key", 800, commitKey); });
    var label = $("label");
    var commitLabel = function () {
      var v = clean(label.value, 60);
      if (v === S.settings.label) return;
      S.settings.label = v;
      saveSettings();
    };
    label.addEventListener("change", commitLabel);
    label.addEventListener("input", function () { later("label", 800, commitLabel); });
    var tool = $("tool-path");
    tool.addEventListener("change", function () {
      S.global.toolPath = clean(tool.value, 4096);
      saveGlobal();
    });
    $("refresh").addEventListener("click", requestApps);
  }

  function onMessage(evt) {
    var msg;
    try { msg = JSON.parse(evt.data); } catch (e) { return; }
    if (!msg || typeof msg !== "object") return;
    var payload = (msg.payload && typeof msg.payload === "object") ? msg.payload : {};
    if (msg.event === "sendToPropertyInspector" && payload.type === "apps") {
      S.apps = Array.isArray(payload.apps) ? payload.apps.filter(function (a) {
        return a && typeof a.key === "string" && a.key;
      }) : [];
      S.toolRunning = !!payload.toolRunning;
      S.capture = typeof payload.capture === "string" ? payload.capture : "";
      S.enforce = !!payload.enforce;
      render();
    } else if (msg.event === "didReceiveSettings") {
      var s = (payload.settings && typeof payload.settings === "object") ? payload.settings : {};
      S.settings = { app: clean(s.app, 200), label: clean(s.label, 60) };
      fillForm();
    } else if (msg.event === "didReceiveGlobalSettings") {
      var g = (payload.settings && typeof payload.settings === "object") ? payload.settings : {};
      S.global = { toolPath: clean(g.toolPath, 4096) };
      fillForm();
    }
  }

  function connect(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    if (S.ws) return;  // OpenDeck może zawołać i wersję OpenAction, i Elgato — łączymy się raz
    S.uuid = inUUID;
    var info = {};
    try { info = JSON.parse(inActionInfo || "{}") || {}; } catch (e) { info = {}; }
    S.action = info.action || "io.github.gawson.warpipe.app";
    var s = (info.payload && info.payload.settings) || {};
    S.settings = { app: clean(s.app, 200), label: clean(s.label, 60) };
    fillForm();
    S.ws = new WebSocket("ws://127.0.0.1:" + inPort);
    S.ws.onopen = function () {
      send({ event: inRegisterEvent, uuid: inUUID });
      send({ event: "getGlobalSettings", context: inUUID });
      requestApps();
    };
    S.ws.onmessage = onMessage;
    S.ws.onclose = function () {
      S.toolRunning = null;
      var p = $("status");
      if (p) { p.textContent = "Brak połączenia z OpenDeck."; p.className = "hint bad"; }
    };
  }

  if (typeof document !== "undefined" && document.getElementById("app-select")) bind();

  return {
    clean: clean,
    buildOptions: buildOptions,
    statusLine: statusLine,
    connect: connect,
    _state: S
  };
})();

function connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
  DDPI.connect(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo);
}

function connectOpenActionSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
  DDPI.connect(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo);
}

if (typeof module !== "undefined" && module.exports) module.exports = DDPI;
