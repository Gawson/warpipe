/* Warpipe — settings of the "App audio" key (property inspector).
 *
 * After loading this page OpenDeck calls connectOpenActionSocket(...) (if it exists)
 * or connectElgatoStreamDeckSocket(...); the Elgato software calls
 * connectElgatoStreamDeckSocket(...). The arguments are the same:
 *   (port, uuid of this inspector, "registerPropertyInspector", info JSON, actionInfo JSON)
 *
 * Language: English by default, Polish when the plugin says so ("lang" in its
 * "apps" payload, which follows the Warpipe setting); before that, the host's
 * application.language is used as a first guess.
 *
 * No external libraries, no internet.
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
    enforce: false,
    lang: "en"
  };
  var timers = {};

  // ------------------------------------------------------------ translations
  var T = {
    en: {
      title: "App audio",
      app: "App",
      refresh: "Refresh the list",
      orType: "or type a name",
      keyPlaceholder: "e.g. Warframe.x64.exe (when not playing)",
      keyLabel: "Key label",
      labelPlaceholder: "(optional) e.g. Warframe",
      connecting: "Connecting to the plugin…",
      help: "Pressing the key adds or removes this app's audio from your Discord stream. " +
            "Green = the app is on stream now. Grey = the app is not playing.",
      advanced: "Advanced",
      toolPath: "warpipe program",
      toolPlaceholder: "(empty = find it automatically)",
      toolHelp: "Full path, e.g. ~/.local/bin/warpipe. Only needed when the plugin can't " +
                "start the program by itself.",
      pick: "— pick from the list —",
      noneNow: "— no app is playing right now —",
      unavailable: "— list unavailable —",
      optOnStream: "{app} — on stream",
      optNotPlaying: "{app} (not playing now)",
      notRunning: "Warpipe is not running. Press the key on the Stream Deck to start it " +
                  "(or start it from the app menu).",
      capActive: "Discord Go Live audio: active.",
      capAmbiguous: "Discord Go Live audio: ambiguous — pick it in the Warpipe tray menu.",
      capNone: "Discord Go Live audio: not detected. Your choice takes effect when you start " +
               "a Go Live stream with sound.",
      appOnStream: "{app}: on stream.",
      appNotPlaying: "{app}: not playing now.",
      appOff: "{app}: playing, but not going to the stream.",
      pickApp: "Pick an app from the list.",
      noOpenDeck: "No connection to OpenDeck."
    },
    pl: {
      title: "Dźwięk z aplikacji",
      app: "Aplikacja",
      refresh: "Odśwież listę",
      orType: "albo wpisz nazwę",
      keyPlaceholder: "np. Warframe.x64.exe (gdy teraz nie gra)",
      keyLabel: "Podpis na przycisku",
      labelPlaceholder: "(opcjonalnie) np. Warframe",
      connecting: "Łączę się z wtyczką…",
      help: "Naciśnięcie przycisku dodaje albo zabiera dźwięk tej aplikacji ze streamu w Discordzie. " +
            "Zielony przycisk = aplikacja jest teraz na streamie. Szary = aplikacja teraz nie gra.",
      advanced: "Zaawansowane",
      toolPath: "Program warpipe",
      toolPlaceholder: "(puste = znajdź sam)",
      toolHelp: "Pełna ścieżka, np. ~/.local/bin/warpipe. Potrzebne tylko wtedy, gdy wtyczka " +
                "nie umie sama uruchomić programu.",
      pick: "— wybierz z listy —",
      noneNow: "— żadna aplikacja teraz nie gra —",
      unavailable: "— lista niedostępna —",
      optOnStream: "{app} — na streamie",
      optNotPlaying: "{app} (teraz nie gra)",
      notRunning: "Program „Warpipe” nie działa. Naciśnij przycisk na Stream Decku, " +
                  "żeby go uruchomić (albo uruchom go z menu programów).",
      capActive: "Stream z dźwiękiem: wykryty.",
      capAmbiguous: "Stream z dźwiękiem: niepewny — wybierz go w menu ikony programu.",
      capNone: "Stream z dźwiękiem: nie wykryty. Wybór zadziała sam, gdy zaczniesz stream z dźwiękiem.",
      appOnStream: "{app}: na streamie.",
      appNotPlaying: "{app}: teraz nie gra.",
      appOff: "{app}: gra, ale nie idzie do streamu.",
      pickApp: "Wybierz aplikację z listy.",
      noOpenDeck: "Brak połączenia z OpenDeck."
    }
  };

  function t(key, vars) {
    var table = T[S.lang] || T.en;
    var s = table.hasOwnProperty(key) ? table[key] : (T.en.hasOwnProperty(key) ? T.en[key] : key);
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, function (m, k) {
      return Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m;
    });
  }

  // "en" / "pl", or null for "auto" / unknown input (same rules as the plugin and the tool)
  function normLang(v) {
    if (typeof v !== "string") return null;
    v = v.trim().toLowerCase();
    if (!v || v === "auto") return null;
    return v.indexOf("pl") === 0 ? "pl" : "en";
  }

  function eachEl(selector, fn) {
    if (typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
    var list = document.querySelectorAll(selector);
    for (var i = 0; i < list.length; i++) fn(list[i]);
  }

  function applyStatic() {
    if (typeof document !== "undefined" && document.documentElement) document.documentElement.lang = S.lang;
    eachEl("[data-i18n]", function (el) { el.textContent = t(el.getAttribute("data-i18n")); });
    eachEl("[data-i18n-placeholder]", function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    eachEl("[data-i18n-title]", function (el) { el.setAttribute("title", t(el.getAttribute("data-i18n-title"))); });
  }

  function setLang(lang) {
    var l = normLang(lang) || "en";
    if (l !== S.lang) {
      S.lang = l;
      applyStatic();
    }
    return S.lang;
  }

  // ------------------------------------------------------------ pure functions
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

  // Drop-down options: [{value, text, selected}]
  function buildOptions(apps, current, toolRunning) {
    var present = apps.filter(function (a) { return a.present; });
    var first = present.length ? t("pick") : (toolRunning ? t("noneNow") : t("unavailable"));
    var opts = [{ value: "", text: first, selected: !current }];
    present.forEach(function (a) {
      var name = a.display || a.key;
      opts.push({
        value: a.key,
        text: a.in_stream ? t("optOnStream", { app: name }) : name,
        selected: !!current && sameKey(a.key, current)
      });
    });
    if (current && !findApp(present, current)) {
      opts.push({ value: current, text: t("optNotPlaying", { app: current }), selected: true });
    }
    return opts;
  }

  function statusLine(toolRunning, capture, apps, current) {
    if (toolRunning === null) return { text: t("connecting"), cls: "" };
    if (!toolRunning) return { text: t("notRunning"), cls: "bad" };
    var text, cls;
    if (capture === "active") { text = t("capActive"); cls = "ok"; }
    else if (capture === "ambiguous") { text = t("capAmbiguous"); cls = "warn"; }
    else { text = t("capNone"); cls = "warn"; }
    if (current) {
      var a = findApp(apps, current);
      if (!a || !a.present) text += " " + t("appNotPlaying", { app: current });
      else if (a.in_stream) text += " " + t("appOnStream", { app: current });
      else text += " " + t("appOff", { app: current });
    } else {
      text += " " + t("pickApp");
    }
    return { text: text, cls: cls };
  }

  // ------------------------------------------------------------ messaging
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
      el.textContent = o.text;   // textContent: app names never as HTML
      if (o.selected) el.selected = true;
      sel.appendChild(el);
    });
    var st = statusLine(S.toolRunning, S.capture, S.apps, S.settings.app);
    var p = $("status");
    p.textContent = st.text;
    p.className = "hint" + (st.cls ? " " + st.cls : "");
  }

  function fillForm() {
    var k = $("app-key"), l = $("label"), tp = $("tool-path");
    if (k && document.activeElement !== k) k.value = S.settings.app || "";
    if (l && document.activeElement !== l) l.value = S.settings.label || "";
    if (tp && document.activeElement !== tp) tp.value = S.global.toolPath || "";
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
      if (typeof payload.lang === "string") setLang(payload.lang);
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
    if (S.ws) return;  // OpenDeck may call both the OpenAction and the Elgato variant — connect once
    S.uuid = inUUID;
    var host = {};
    try { host = JSON.parse(inInfo || "{}") || {}; } catch (e) { host = {}; }
    var hostLang = host.application && host.application.language;
    if (normLang(hostLang) === "pl") setLang("pl");   // first guess until the plugin tells us
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
      if (p) { p.textContent = t("noOpenDeck"); p.className = "hint bad"; }
    };
  }

  if (typeof document !== "undefined" && document.getElementById("app-select")) bind();

  return {
    clean: clean,
    buildOptions: buildOptions,
    statusLine: statusLine,
    setLang: setLang,
    t: t,
    T: T,
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
