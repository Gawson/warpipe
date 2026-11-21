#!/usr/bin/python3 -I
"""Live test scenario: OpenDeck plugin <-> tool (--watch) <-> PipeWire.

Run ONLY via tests/live_plugin.sh (it sets up test directories, cleans up and
verifies afterwards). Argument: the test's temporary directory.

We create only our own objects (prefix ddp-test): a null sink, fake apps playing
silence from /dev/zero, fake Discord streams recording to /dev/null. The tool gets
a config in which "Discord" = only the ddp-test-discord* nodes. Every started
process is recorded in <tmp>/pids (the bash script cleans them up).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

from fake_opendeck import (ACTION_APP, ACTION_SYSTEM, FakeOpenDeck,  # noqa: E402
                           context_for, plugin_env)

TMP = sys.argv[1]
TOOL = [sys.executable, os.path.join(ROOT, "bin", "warpipe")]
CONFIG_DIR = os.environ["DISCORD_DZWIEK_CONFIG_DIR"]
RUN_DIR = os.environ["DISCORD_DZWIEK_RUNTIME_DIR"]
assert CONFIG_DIR.startswith(TMP) and RUN_DIR.startswith(TMP), "the tool directories must be test directories"
SOCK = os.path.join(RUN_DIR, "control.sock")
SINK = "ddp-test-sink"
COMMON = "state.restore-props = false, state.restore-target = false"
GAME, BROWSER, MUSIC = "ddp-test-game", "ddp-test-browser", "ddp-test-music"
CAP = "ddp-test-discord_capture"

PASSES = 0
FAILS = 0
procs: dict[str, subprocess.Popen] = {}
module_id: str | None = None


def say(kind: str, msg: str) -> None:
    print(f"{kind}: {msg}", flush=True)


def check(desc: str, cond) -> bool:
    global PASSES, FAILS
    try:
        ok = cond() if callable(cond) else bool(cond)
    except Exception as e:  # noqa: BLE001
        ok = False
        first = str(e).strip().splitlines()[0] if str(e).strip() else ""
        desc += f" [{type(e).__name__}: {first[:300]}]"
        with open(os.path.join(TMP, "failures.log"), "a") as f:
            f.write(f"--- {desc}\n{e}\n")
    if ok:
        PASSES += 1
        say("PASS", desc)
    else:
        FAILS += 1
        say("FAIL", desc)
    return ok


class Abort(Exception):
    pass


def abort(msg: str) -> None:
    say("FAIL", msg)
    raise Abort(msg)


def record_pid(name: str, p: subprocess.Popen) -> None:
    procs[name] = p
    with open(os.path.join(TMP, "pids"), "a") as f:
        f.write(f"{p.pid} {name}\n")


# --------------------------------------------------------------------------
# PipeWire (tylko odczyt + własne obiekty)
# --------------------------------------------------------------------------

def dump() -> list:
    r = subprocess.run(["pw-dump", "-N"], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(r.stdout)


def graph():
    d = dump()
    nodes = {o["id"]: o for o in d if o.get("type") == "PipeWire:Interface:Node" and o.get("info")}
    ports = {o["id"]: o for o in d if o.get("type") == "PipeWire:Interface:Port" and o.get("info")}
    links = [o for o in d if o.get("type") == "PipeWire:Interface:Link" and o.get("info")]
    return nodes, ports, links


def props(o) -> dict:
    return o["info"].get("props") or {}


def ids(name: str) -> list[int]:
    nodes, _, _ = graph()
    return sorted(i for i, n in nodes.items() if props(n).get("node.name") == name)


def serial(node_id: int) -> int:
    nodes, _, _ = graph()
    return int(props(nodes[node_id])["object.serial"])


def inbound_src(node_id: int) -> set[str]:
    nodes, _, links = graph()
    out = set()
    for l in links:
        if l["info"]["input-node-id"] == node_id:
            src = nodes.get(l["info"]["output-node-id"])
            out.add(props(src).get("node.name") if src else "?")
    return out


def inbound_raw(node_id: int) -> list[str]:
    nodes, ports, links = graph()
    out = []
    for l in links:
        i = l["info"]
        if i["input-node-id"] == node_id:
            out.append(f'{props(nodes[i["output-node-id"]]).get("node.name")}:'
                       f'{props(ports[i["output-port-id"]]).get("port.name")}>'
                       f'{props(ports[i["input-port-id"]]).get("port.name")}')
    return sorted(out)


def ours_count() -> int:
    """Nasze DODATKOWE połączenia (znacznik managed). Odtworzone oryginały mają znacznik
    „restored” i zostają po przywróceniu — to znowu stan sprzed zmian (jak w live_pipewire.sh)."""
    _, _, links = graph()
    return sum(1 for l in links if "warpipe.managed" in props(l))


def wait(pred, timeout: float = 5.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while True:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() > end:
            return False
        time.sleep(step)


def wait_node(name: str, count: int = 1, timeout: float = 5.0) -> list[int]:
    if not wait(lambda: len(ids(name)) >= count, timeout):
        abort(f"węzeł {name} się nie pojawił")
    return ids(name)


def start_app(name: str) -> int:
    before = set(ids(name))
    p = subprocess.Popen(
        ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
         "--target", SINK, "-P",
         f'{{ node.name = "{name}", application.name = "{name}", media.name = "ddp-test playback", {COMMON} }}',
         "/dev/zero"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record_pid(name, p)
    if not wait(lambda: set(ids(name)) - before, 5):
        abort(f"atrapa {name} się nie pojawiła")
    return sorted(set(ids(name)) - before)[0]


def start_capture(key: str, target_serial: int) -> int:
    before = set(ids(CAP))
    p = subprocess.Popen(
        ["pw-cat", "--record", "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
         "--target", str(target_serial), "-P",
         f'{{ node.name = "{CAP}", application.name = "{CAP}", application.process.binary = "Discord", '
         f'media.name = "ddp-test game capture", node.dont-fallback = true, node.dont-reconnect = true, {COMMON} }}',
         "/dev/null"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record_pid(key, p)
    if not wait(lambda: set(ids(CAP)) - before, 5):
        abort(f"atrapa przechwytywania {key} się nie pojawiła")
    return sorted(set(ids(CAP)) - before)[0]


def stop_proc(name: str, sig=signal.SIGTERM, timeout: float = 5.0):
    p = procs.pop(name, None)
    if p is None:
        return None
    if p.poll() is None:
        p.send_signal(sig)
        try:
            return p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return p.wait(3)
    return p.returncode


def tool(*args, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(TOOL + list(args), capture_output=True, text=True, timeout=timeout)


def safe_guard() -> None:
    r = tool("--list", "--json")
    if r.returncode != 0:
        abort(f"--list --json nie działa: {r.stderr.strip()}")
    names = [c["node_name"] for c in json.loads(r.stdout)["candidates"]]
    if not all(n.startswith("ddp-test") for n in names):
        abort(f"narzędzie widzi strumienie spoza testu: {names}")


# --------------------------------------------------------------------------

def scenario() -> None:
    global module_id
    with open(os.path.join(CONFIG_DIR, "config.json"), "w") as f:
        json.dump({"version": 1, "selected_apps": [], "enforce": False,
                   "capture_match_terms": ["ddp-test-discord"],
                   "capture_names": [CAP],
                   "mic_names": ["ddp-test-mic", "recStream", "WEBRTC VoiceEngine"]}, f)

    r = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
                        f"sink_properties=state.restore-props=false state.restore-target=false "
                        f"device.description={SINK}"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        abort(f"nie udało się utworzyć null sink: {r.stderr.strip()}")
    module_id = r.stdout.strip()
    with open(os.path.join(TMP, "module"), "w") as f:
        f.write(module_id)
    sink_id = wait_node(SINK)[0]
    say("INFO", f"null sink #{sink_id} (moduł {module_id})")

    game = start_app(GAME)
    browser = start_app(BROWSER)
    music = start_app(MUSIC)
    say("INFO", f"aplikacje: game #{game}, browser #{browser}, music #{music}")

    # atrapa mikrofonu rozmowy (recStream), podłączona do monitora null sinka — nie wolno jej ruszyć
    p = subprocess.Popen(
        ["pw-cat", "--record", "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
         "--target", "0", "-P",
         f'{{ node.name = "ddp-test-mic", application.name = "ddp-test-discord-voice", '
         f'application.process.binary = "Discord", media.name = "recStream", {COMMON} }}', "/dev/null"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record_pid("mic", p)
    mic = wait_node("ddp-test-mic")[0]
    nodes, ports, _ = graph()

    def port(nid, name):
        return next(i for i, p in ports.items() if props(p).get("node.id") == nid and props(p).get("port.name") == name)
    for ch in ("FL", "FR"):
        subprocess.run(["pw-link", str(port(sink_id, f"monitor_{ch}")), str(port(mic, f"input_{ch}"))],
                       capture_output=True, timeout=10)
    mic_links = inbound_raw(mic)
    if not mic_links:
        abort("nie udało się podłączyć atrapy mikrofonu")

    def mic_untouched():
        return inbound_raw(mic) == mic_links

    cap_g = start_capture("capG", serial(game))
    cap_b = start_capture("capB", serial(browser))
    cap_m = start_capture("capM", serial(music))
    for cap, app in ((cap_g, GAME), (cap_b, BROWSER), (cap_m, MUSIC)):
        if not wait(lambda: inbound_src(cap) == {app}, 5):
            abort(f"WirePlumber nie połączył {app} -> #{cap}")
    say("INFO", f"discord_capture: game #{cap_g}, browser #{cap_b}, music #{cap_m}")
    original = {c: inbound_raw(c) for c in (cap_g, cap_b, cap_m)}

    def links_are(g, b, m) -> bool:
        return (inbound_src(cap_g), inbound_src(cap_b), inbound_src(cap_m)) == (g, b, m)

    def all_original() -> bool:
        return {c: inbound_raw(c) for c in (cap_g, cap_b, cap_m)} == original and ours_count() == 0

    # ------------------------------------------------------------ narzędzie (--watch)
    safe_guard()
    log = open(os.path.join(TMP, "watch.log"), "ab")
    record_pid("watch", subprocess.Popen(TOOL + ["--watch", "-v"], stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL))
    check("T0 --watch: gniazdo sterowania w ciągu 10 s", lambda: wait(lambda: os.path.exists(SOCK), 10))
    st = tool("--status", "--json")
    check("T0 --status: działa, tryb „Cały system”, stream wykryty (3 strumienie)",
          lambda: st.returncode == 0 and json.loads(st.stdout)["enforce"] is False
          and json.loads(st.stdout)["capture"] == {"status": "active", "count": 3})
    check("T0 „Cały system”: nic nie zmienione", all_original)

    # ------------------------------------------------------------ wtyczka
    od = FakeOpenDeck()
    procs["_od"] = od  # type: ignore[assignment]
    od.launch(env=plugin_env(RUN_DIR), log_path=os.path.join(TMP, "plugin.log"))
    with open(os.path.join(TMP, "pids"), "a") as f:
        f.write(f"{od.proc.pid} plugin\n")
    uuid = od.wait_registered()
    check("W0 wtyczka zarejestrowana jak w OpenDeck", uuid == "io.github.gawson.warpipe.sdPlugin")
    kg, kb, km, kx, ks = (context_for(i) for i in range(5))
    keys = {kg: {"app": GAME}, kb: {"app": BROWSER}, km: {"app": MUSIC}, kx: {"app": "ddp-test-nieobecna"}}
    for k, s in keys.items():
        od.will_appear(k, ACTION_APP, s)
    od.will_appear(ks, ACTION_SYSTEM, {})

    def imgs(expect: dict, timeout: float = 6.0) -> bool:
        for k, state in expect.items():
            ring = None
            if isinstance(state, tuple):
                state, ring = state
            od.wait_image(k, state, timeout=timeout, ring=ring)
        return True

    check("W1 „Cały system”: game/browser/music zielone (na streamie), nieobecna szara, system świeci",
          lambda: imgs({kg: "app:on", kb: "app:on", km: "app:on", kx: "app:absent", ks: "system:sys_on"}))
    img = od.last_image(kx)
    check("W1 absent: caption = name, badge \"not playing\"",
          img and img["label"] == "ddp-test-nieobecna" and img["badge"] == "not playing")

    # ------------------------------------------------------------ naciśnięcia → prawdziwe połączenia
    od.key_down(kg, ACTION_APP, keys[kg])
    check("W2 naciśnięcie game: tylko game na streamie (obrazki)",
          lambda: imgs({kg: "app:on", kb: "app:off", km: "app:off", ks: "system:sys_off"}))
    check("W2 pw-dump: capG <- game, capB i capM puste",
          lambda: wait(lambda: links_are({GAME}, set(), set()), 3))
    check("W2 mikrofon nietknięty", mic_untouched)
    with open(os.path.join(CONFIG_DIR, "config.json")) as f:
        cfg = json.load(f)
    check("W2 wybór zapisany w konfiguracji (enforce + [game])",
          cfg.get("enforce") is True and cfg.get("selected_apps") == [GAME])

    od.key_down(km, ACTION_APP, keys[km])
    check("W3 + music: game i music na streamie", lambda: imgs({kg: "app:on", km: "app:on", kb: "app:off"}))
    check("W3 pw-dump: capG <- game, capM <- music, capB puste",
          lambda: wait(lambda: links_are({GAME}, set(), {MUSIC}), 3))

    od.key_down(kg, ACTION_APP, keys[kg])
    check("W4 odznaczenie game: game off, music dalej on", lambda: imgs({kg: "app:off", km: "app:on"}))
    check("W4 pw-dump: capG puste, capM <- music", lambda: wait(lambda: links_are(set(), set(), {MUSIC}), 3))

    od.key_down(km, ACTION_APP, keys[km])
    check("W5 odznaczenie ostatniej → „Cały system”: wszystko na streamie, system świeci",
          lambda: imgs({kg: "app:on", kb: "app:on", km: "app:on", ks: "system:sys_on"}))
    check("W5 pw-dump: połączenia jak na początku, brak naszych", lambda: wait(all_original, 3))
    with open(os.path.join(CONFIG_DIR, "config.json")) as f:
        cfg = json.load(f)
    check("W5 konfiguracja: enforce=false, pusty wybór",
          cfg.get("enforce") is False and cfg.get("selected_apps") == [])

    m0 = od.mark()
    od.key_down(kx, ACTION_APP, keys[kx])
    check("W6 aplikacja, która nie gra: showAlert, bez zmian",
          lambda: od.wait_for(lambda m: m.get("event") == "showAlert" and m.get("context") == kx, 3, since=m0)
          and all_original())

    od.key_down(kb, ACTION_APP, keys[kb])
    check("W7 tylko browser", lambda: imgs({kb: "app:on", kg: "app:off", km: "app:off", ks: "system:sys_off"}))
    check("W7 pw-dump: capB <- browser, reszta pusta", lambda: wait(lambda: links_are(set(), {BROWSER}, set()), 3))
    od.key_down(ks, ACTION_SYSTEM, {})
    check("W8 „Cały system” z przycisku: wszystko na streamie",
          lambda: imgs({kg: "app:on", kb: "app:on", km: "app:on", ks: "system:sys_on"}))
    check("W8 pw-dump: przywrócone połączenia jak na początku, brak naszych", lambda: wait(all_original, 3))
    check("W8 mikrofon nietknięty", mic_untouched)

    # ------------------------------------------------------------ inspektor właściwości
    m0 = od.mark()
    od.send_to_plugin(kg, ACTION_APP, {"cmd": "listApps"})
    pi = od.wait_for(lambda m: m.get("event") == "sendToPropertyInspector" and m.get("context") == kg, 5, since=m0)
    keys_pi = [a["key"] for a in pi["payload"]["apps"]]
    check("W9 lista dla inspektora: atrapy aplikacji są, strumieni Discorda/mikrofonu nie ma",
          {GAME, BROWSER, MUSIC} <= set(keys_pi) and not any("discord" in k or "mic" in k for k in keys_pi)
          and pi["payload"]["toolRunning"] and pi["payload"]["capture"] == "active")

    # ------------------------------------------------------------ zmiany z innego miejsca (CLI)
    r = tool("--toggle", MUSIC)
    check("W10 --toggle z wiersza poleceń: kod 0, przycisk podąża (music on, game off)",
          lambda: r.returncode == 0 and imgs({km: "app:on", kg: "app:off", ks: "system:sys_off"}))
    r = tool("--system")
    check("W10 --system: wszystko z powrotem", lambda: r.returncode == 0 and imgs({kg: "app:on", ks: "system:sys_on"})
          and wait(all_original, 3))

    # ------------------------------------------------------------ aplikacja znika i wraca
    stop_proc(BROWSER)
    check("W11 zamknięcie atrapy browser: przycisk szary („brak”)", lambda: imgs({kb: "app:absent"}))
    check("W11 capB bez połączeń", lambda: wait(lambda: inbound_src(cap_b) == set(), 3))
    browser2 = start_app(BROWSER)
    check("W12 browser znowu gra: przycisk dostępny (bez własnego discord_capture → „off”)",
          lambda: imgs({kb: "app:off"}))
    od.key_down(kb, ACTION_APP, keys[kb])
    check("W12 naciśnięcie: nowy browser dołączony do streamu (nośnik), reszta odcięta",
          lambda: imgs({kb: "app:on", kg: "app:off", km: "app:off"}))
    check("W12 pw-dump: browser w jednym ze strumieni Discorda",
          lambda: wait(lambda: any(BROWSER in inbound_src(c) for c in (cap_g, cap_b, cap_m))
                       and GAME not in inbound_src(cap_g) and MUSIC not in inbound_src(cap_m), 3))
    od.key_down(ks, ACTION_SYSTEM, {})
    check("W12 „Cały system”: game/music przywrócone, brak naszych",
          lambda: imgs({ks: "system:sys_on", kg: "app:on", km: "app:on"})
          and wait(lambda: inbound_src(cap_g) == {GAME} and inbound_src(cap_m) == {MUSIC} and ours_count() == 0, 3))
    say("INFO", f"browser #{browser2}")

    # ------------------------------------------------------------ narzędzie nie działa
    od.key_down(kg, ACTION_APP, keys[kg])   # włączone pilnowanie: wyjście musi przywrócić
    imgs({kg: "app:on", km: "app:off"})
    rc = stop_proc("watch")
    check("N1 SIGTERM --watch: kod 0", rc == 0)
    check("N1 przyciski: czerwony „!” (narzędzie nie działa)",
          lambda: imgs({kg: "app:down", kb: "app:down", ks: "system:down"}))
    check("N1 wyjście narzędzia przywróciło połączenia (game/music), brak naszych",
          lambda: wait(lambda: inbound_src(cap_g) == {GAME} and inbound_src(cap_m) == {MUSIC}
                       and ours_count() == 0, 3))
    check("N1 gniazdo sterowania usunięte", not os.path.exists(SOCK))
    check("N1 mikrofon nietknięty", mic_untouched)

    # naciśnięcie uruchamia narzędzie (ścieżka z ustawień wtyczki → atrapa uruchamiacza,
    # która zamiast ikony startuje --watch z tymi samymi katalogami testowymi)
    launcher = os.path.join(TMP, "launcher.sh")
    args_file = os.path.join(TMP, "launcher.args")
    with open(launcher, "w") as f:
        f.write("#!/bin/sh\n"
                f'echo "$@" > "{args_file}"\n'
                f'echo "$$ relaunched-watch" >> "{TMP}/pids"\n'
                f'exec "{sys.executable}" "{os.path.join(ROOT, "bin", "warpipe")}" --watch -v '
                f'>> "{TMP}/watch2.log" 2>&1\n')
    os.chmod(launcher, 0o755)
    od.did_receive_global_settings({"toolPath": launcher})
    time.sleep(0.3)
    od.key_down(kg, ACTION_APP, keys[kg])
    check("N2 naciśnięcie przy wyłączonym narzędziu: „uruchamiam…”", lambda: imgs({kg: "app:starting"}, 3))
    check("N2 wtyczka uruchomiła program z --tray (bez powłoki)",
          lambda: wait(lambda: open(args_file).read().strip() == "--tray", 5))
    # nowe narzędzie czyta zapisany wybór (pilnowanie włączone, tylko game) i od razu go stosuje
    check("N3 narzędzie wstało, wtyczka połączyła się sama: przyciski wracają (tylko game)",
          lambda: imgs({kg: "app:on", km: "app:off", kb: "app:off", ks: "system:sys_off"}, 15))
    check("N3 pw-dump: capG <- game, capM puste (zapamiętany wybór zastosowany)",
          lambda: wait(lambda: inbound_src(cap_g) == {GAME} and inbound_src(cap_m) == set(), 5))
    relaunched = None
    with open(os.path.join(TMP, "pids")) as f:
        for line in f:
            pid, _, name = line.strip().partition(" ")
            if name == "relaunched-watch":
                relaunched = int(pid)
    check("N3 nowe narzędzie działa w osobnej sesji (nie jako dziecko wtyczki)",
          relaunched is not None and os.getsid(relaunched) == relaunched)
    if relaunched:
        os.kill(relaunched, signal.SIGTERM)
        check("N4 zatrzymanie uruchomionego narzędzia", lambda: wait(lambda: not os.path.exists(f"/proc/{relaunched}")
                                                                     or open(f"/proc/{relaunched}/stat").read().split()[2] == "Z", 8))
        check("N4 przyciski znowu czerwone", lambda: imgs({kg: "app:down"}))
        check("N4 wyjście przywróciło music, brak naszych",
              lambda: wait(lambda: inbound_src(cap_m) == {MUSIC} and ours_count() == 0, 5))

    # ------------------------------------------------------------ koniec OpenDeck
    proc = od.proc
    rc = od.stop()
    procs.pop("_od", None)
    check("Z1 OpenDeck zamknął połączenie: wtyczka kończy się sama (kod 0)", rc == 0 and proc.poll() == 0)
    check("Z1 wszystko jak na początku (połączenia strumieni Discorda, brak naszych)",
          lambda: inbound_src(cap_g) == {GAME} and inbound_src(cap_m) == {MUSIC} and ours_count() == 0)
    check("Z1 mikrofon nietknięty", mic_untouched)
    with open(os.path.join(TMP, "plugin.log"), errors="replace") as f:
        plog = f.read()
    check("Z1 log wtyczki bez błędów (Traceback)", "Traceback" not in plog)
    for line in plog.strip().splitlines()[-6:]:
        say("INFO", "wtyczka: " + line)


def cleanup() -> None:
    od = procs.pop("_od", None)
    if od is not None:
        try:
            od.stop()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    for name in list(procs):
        try:
            stop_proc(name)
        except Exception:  # noqa: BLE001
            pass
    if module_id:
        subprocess.run(["pactl", "unload-module", module_id], capture_output=True, timeout=10)


def main() -> int:
    try:
        scenario()
    except Abort:
        pass
    except Exception as e:  # noqa: BLE001
        global FAILS
        FAILS += 1
        say("FAIL", f"wyjątek: {type(e).__name__}: {e}")
    finally:
        cleanup()
    print(f"=== scenariusz: PASS={PASSES} FAIL={FAILS}", flush=True)
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
