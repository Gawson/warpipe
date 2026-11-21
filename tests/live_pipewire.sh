#!/bin/bash
# Self-contained test against a live PipeWire (PipeWire + WirePlumber).
#
# Hard rules:
#  - only OUR OWN objects: null sink "dd-test-sink" (pactl load-module), fake apps
#    (pw-cat playing SILENCE from /dev/zero into dd-test-sink), fake Discord streams
#    (pw-cat --record to /dev/null); nothing touches existing nodes or devices;
#  - the tool gets a test config in which "Discord" = only dd-test-discord* nodes,
#    so even automatic detection never sees the real Discord/Vesktop;
#  - before every change we check that the detected group consists of dd-test nodes only;
#  - cleanup in trap EXIT (also on error), verified with pw-dump;
#  - all fakes have state.restore-props/target=false, so WirePlumber remembers nothing
#    (checksums of ~/.local/state/wireplumber/* are compared).
#
# Output: PASS:/FAIL:/INFO: lines and a summary; exit code 0 = everything passed.
# (Check descriptions below are still partly in Polish.)

set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
TOOL=(timeout 25 python3 "$HERE/bin/warpipe")
BASE_TMP=${WARPIPE_TEST_TMP:-${XDG_RUNTIME_DIR:-/tmp}}
mkdir -p "$BASE_TMP"
TMP=$(mktemp -d "$BASE_TMP/dd-live.XXXXXX")
export WARPIPE_CONFIG_DIR="$TMP/config"
export WARPIPE_RUNTIME_DIR="$TMP/run"
export WARPIPE_LANG=en   # the checks below grep English output
mkdir -p "$WARPIPE_CONFIG_DIR" "$WARPIPE_RUNTIME_DIR"

SINK=dd-test-sink
MOD=""
declare -A PID_OF=()
WATCH_PID=""
KILL_PID=""
PASSES=0
FAILS=0
WP_STATE_DIR="$HOME/.local/state/wireplumber"

pass() { echo "PASS: $*"; PASSES=$((PASSES + 1)); }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
info() { echo "INFO: $*"; }
check() { # check "opis" warunek...
    local desc=$1; shift
    if "$@"; then pass "$desc"; else fail "$desc"; fi
}

# ---------------------------------------------------------------- pomocnik
cat > "$TMP/q.py" <<'PY'
import json, subprocess, sys, time

def dump():
    return json.loads(subprocess.run(["pw-dump", "-N"], capture_output=True, text=True, check=True).stdout)

def graph():
    d = dump()
    nodes = {o["id"]: o for o in d if o.get("type") == "PipeWire:Interface:Node" and o.get("info")}
    ports = {o["id"]: o for o in d if o.get("type") == "PipeWire:Interface:Port" and o.get("info")}
    links = [o for o in d if o.get("type") == "PipeWire:Interface:Link" and o.get("info")]
    return d, nodes, ports, links

def nname(nodes, i):
    n = nodes.get(i)
    return n["info"]["props"].get("node.name") if n else "?"

def pname(ports, i):
    p = ports.get(i)
    return p["info"]["props"].get("port.name") if p else "?"

def inbound(node_id):
    _, nodes, ports, links = graph()
    out = []
    for l in links:
        i = l["info"]
        if i["input-node-id"] == node_id:
            out.append(f'{nname(nodes, i["output-node-id"])}#{i["output-node-id"]}:{pname(ports, i["output-port-id"])}>{pname(ports, i["input-port-id"])}')
    return sorted(out)

cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "ids":            # ids <node.name>
    _, nodes, _, _ = graph()
    print(" ".join(str(i) for i, n in sorted(nodes.items()) if n["info"]["props"].get("node.name") == args[0]))
elif cmd == "serial":       # serial <node-id>
    _, nodes, _, _ = graph()
    print(nodes[int(args[0])]["info"]["props"]["object.serial"])
elif cmd == "port":         # port <node-id> <port.name>
    _, _, ports, _ = graph()
    print(next(i for i, p in ports.items() if p["info"]["props"].get("node.id") == int(args[0]) and p["info"]["props"].get("port.name") == args[1]))
elif cmd == "ports":        # ports <node-id>
    _, _, ports, _ = graph()
    print(" ".join(sorted(p["info"]["props"].get("port.name") for p in ports.values() if p["info"]["props"].get("node.id") == int(args[0]))))
elif cmd == "inbound":      # inbound <node-id>
    print(" ".join(inbound(int(args[0]))))
elif cmd == "inbound-src":  # inbound-src <node-id> -> nazwy węzłów źródłowych (unikalne)
    print(" ".join(sorted({s.split("#")[0] for s in inbound(int(args[0]))})))
elif cmd == "wait-node":    # wait-node <node.name> <min-count> <timeout>
    end = time.time() + float(args[2])
    while time.time() < end:
        _, nodes, _, _ = graph()
        if sum(1 for n in nodes.values() if n["info"]["props"].get("node.name") == args[0]) >= int(args[1]):
            sys.exit(0)
        time.sleep(0.1)
    sys.exit(1)
elif cmd == "wait-gone":    # wait-gone <node-id> <timeout>
    end = time.time() + float(args[1])
    while time.time() < end:
        _, nodes, _, _ = graph()
        if int(args[0]) not in nodes:
            sys.exit(0)
        time.sleep(0.1)
    sys.exit(1)
elif cmd == "wait-inbound": # wait-inbound <node-id> <oczekiwane-źródła ("" = brak)> <timeout>
    want = args[1]
    end = time.time() + float(args[2])
    while True:
        got = " ".join(sorted({s.split("#")[0] for s in inbound(int(args[0]))}))
        if got == want:
            sys.exit(0)
        if time.time() > end:
            print(got)
            sys.exit(1)
        time.sleep(0.1)
elif cmd == "link-lifetime":  # link-lifetime <src node.name> <dst node.name> <timeout>
    # Watches pw-dump -m: how long (ms) did links <src> -> <dst> exist? Prints the longest,
    # "unseen" if none showed up (too short-lived for pw-dump -m), "timeout" if one stayed.
    import os, select
    src, dst, timeout = args[0], args[1], float(args[2])
    mon = subprocess.Popen(["pw-dump", "-m", "-N"], stdout=subprocess.PIPE)
    names, links, added, lifetimes = {}, {}, {}, []
    end = time.monotonic() + timeout
    text = ""
    try:
        while time.monotonic() < end and not (lifetimes and not added):
            r, _, _ = select.select([mon.stdout], [], [], 0.1)
            if not r:
                continue
            chunk = os.read(mon.stdout.fileno(), 65536)
            if not chunk:
                break
            now = time.monotonic()
            text += chunk.decode("utf-8", "replace")
            while "\n]\n" in text:
                part, text = text.split("\n]\n", 1)
                for o in json.loads(part + "\n]"):
                    oid, info = o.get("id"), o.get("info")
                    if info is None:
                        if oid in added:
                            lifetimes.append((now - added.pop(oid)) * 1000)
                        names.pop(oid, None)
                        links.pop(oid, None)
                    elif o.get("type") == "PipeWire:Interface:Node" and (info.get("props") or {}).get("node.name"):
                        names[oid] = info["props"]["node.name"]
                    elif o.get("type") == "PipeWire:Interface:Link" and oid not in links:
                        links[oid] = (info.get("output-node-id"), info.get("input-node-id"))
                        if names.get(links[oid][0]) == src and names.get(links[oid][1]) == dst:
                            added[oid] = now
    finally:
        mon.terminate()
        mon.wait()
    if added:
        print("timeout")
        sys.exit(1)
    print(f"{max(lifetimes):.0f}" if lifetimes else "unseen")
elif cmd == "managed":      # liczba naszych (oznaczonych) połączeń w całym grafie
    _, _, _, links = graph()
    print(sum(1 for l in links if "warpipe.managed" in (l["info"].get("props") or {})))
elif cmd == "leftovers":    # obiekty testu, które zostały
    d, nodes, _, links = graph()
    bad = [f'node#{i}:{n["info"]["props"].get("node.name")}' for i, n in nodes.items()
           if "dd-test" in json.dumps(n["info"]["props"])]
    bad += [f'link#{l["id"]}' for l in links if "warpipe.managed" in (l["info"].get("props") or {})
            or "warpipe.restored" in (l["info"].get("props") or {})]
    print(" ".join(bad))
elif cmd == "foreign":      # połączenia niezwiązane z testem (porty po serialach)
    _, nodes, ports, links = graph()
    test_nodes = {i for i, n in nodes.items() if "dd-test" in json.dumps(n["info"]["props"])}
    out = []
    for l in links:
        i = l["info"]
        if i["output-node-id"] in test_nodes or i["input-node-id"] in test_nodes:
            continue
        out.append(f'{nname(nodes, i["output-node-id"])}:{pname(ports, i["output-port-id"])}>{nname(nodes, i["input-node-id"])}:{pname(ports, i["input-port-id"])}')
    print("\n".join(sorted(out)))
elif cmd == "group-safe":   # group-safe <plik --list --json>: wszystkie wykryte/kandydaci to dd-test
    data = json.load(open(args[0]))
    names = [c["node_name"] for c in data["candidates"]]
    ok = all(n.startswith("dd-test") for n in names)
    print(" ".join(names))
    sys.exit(0 if ok else 1)
elif cmd == "restored":     # liczba odtworzonych przez narzędzie oryginałów (znacznik restored)
    _, _, _, links = graph()
    print(sum(1 for l in links if "warpipe.restored" in (l["info"].get("props") or {})))
elif cmd == "instance":     # tożsamość uruchomienia PipeWire (jak Graph.instance_id)
    d = dump()
    core = next(o for o in d if o.get("type") == "PipeWire:Interface:Core" and o.get("id") == 0)
    print(f'{core["info"]["cookie"]}:{core["info"]["props"].get("application.process.id")}')
elif cmd == "write-snapshot":  # write-snapshot <plik> <instance> <cap-id> <out-node-id> <out-port> <in-port> [pole=wartość ...]
    path, inst, cap, onode, oport, iport = args[:6]
    _, nodes, ports, _ = graph()
    cap, onode = int(cap), int(onode)
    def port(nid, name):
        return next(p for p in ports.values() if p["info"]["props"].get("node.id") == nid and p["info"]["props"].get("port.name") == name)
    op, ip = port(onode, oport), port(cap, iport)
    e = {"out_port_serial": op["info"]["props"]["object.serial"], "in_port_serial": ip["info"]["props"]["object.serial"],
         "out_node_serial": nodes[onode]["info"]["props"]["object.serial"],
         "in_node_serial": nodes[cap]["info"]["props"]["object.serial"],
         "out_node_name": nodes[onode]["info"]["props"].get("node.name"),
         "in_node_name": nodes[cap]["info"]["props"].get("node.name"),
         "out_port_name": oport, "in_port_name": iport, "deleted": True}
    for kv in args[6:]:
        k, v = kv.split("=", 1)
        e[k] = v
    snap = {"version": 2, "pw_instance": inst, "captures": {
        str(nodes[cap]["info"]["props"]["object.serial"]): {"node_name": nodes[cap]["info"]["props"].get("node.name"), "links": [e]}}}
    json.dump(snap, open(path, "w"))
else:
    sys.exit(f"nieznane: {cmd}")
PY
q() { python3 "$TMP/q.py" "$@"; }

# ---------------------------------------------------------------- sprzątanie
kill_pid() {
    local p=$1
    [ -z "$p" ] && return
    kill "$p" 2>/dev/null
    for _ in $(seq 1 30); do kill -0 "$p" 2>/dev/null || return; sleep 0.1; done
    kill -9 "$p" 2>/dev/null
}

cleanup() {
    local rc=$?
    set +e
    trap - EXIT INT TERM
    echo "--- sprzątanie"
    if [ -n "$WATCH_PID" ]; then kill -TERM "$WATCH_PID" 2>/dev/null; sleep 1; kill_pid "$WATCH_PID"; fi
    if [ -n "$KILL_PID" ]; then kill_pid "$KILL_PID"; fi
    for k in "${!PID_OF[@]}"; do kill_pid "${PID_OF[$k]}"; done
    wait 2>/dev/null
    if [ -n "$MOD" ]; then pactl unload-module "$MOD" 2>/dev/null; fi
    # zapasowo: moduł null-sink o naszej nazwie (gdyby $MOD się zgubił)
    for m in $(pactl list short modules 2>/dev/null | awk -v s="sink_name=$SINK" 'index($0, s) {print $1}'); do
        pactl unload-module "$m" 2>/dev/null
    done
    sleep 0.5
    local left
    left=$(q leftovers 2>/dev/null)
    if [ -z "$left" ]; then pass "sprzątanie: w pw-dump brak węzłów dd-test i naszych połączeń"
    else fail "sprzątanie: zostało: $left"; fi
    if pactl list short modules | grep -q "sink_name=$SINK"; then fail "sprzątanie: moduł null-sink został"
    else pass "sprzątanie: brak modułu $SINK"; fi
    # only the processes this run started (a machine-wide pgrep would see other runs)
    local alive="" p
    for k in "${!PID_OF[@]}"; do
        p=${PID_OF[$k]}
        [ -r "/proc/$p/cmdline" ] && tr '\0' ' ' < "/proc/$p/cmdline" | grep -q -- "pw-cat" && alive="$alive $p"
    done
    if [ -n "$alive" ]; then fail "sprzątanie: działa jeszcze pw-cat z testu:$alive"
    else pass "sprzątanie: brak procesów pw-cat z testu"; fi
    if [ -f "$TMP/wp-state.sha256" ]; then
        if (cd / && sha256sum --quiet -c "$TMP/wp-state.sha256" >/dev/null 2>&1); then
            pass "stan WirePlumbera (~/.local/state/wireplumber) bez zmian"
        else
            fail "stan WirePlumbera zmieniony"
        fi
    fi
    if [ -n "${DEF_SINK:-}" ]; then
        [ "$(pactl get-default-sink)" = "$DEF_SINK" ] && pass "domyślne wyjście bez zmian" || fail "domyślne wyjście zmienione"
        [ "$(pactl get-default-source)" = "$DEF_SOURCE" ] && pass "domyślne wejście bez zmian" || fail "domyślne wejście zmienione"
    fi
    if [ -f "$TMP/foreign-before.txt" ]; then
        q foreign > "$TMP/foreign-after.txt" 2>/dev/null
        if diff -q "$TMP/foreign-before.txt" "$TMP/foreign-after.txt" >/dev/null; then
            info "połączenia spoza testu identyczne przed i po"
        else
            info "połączenia spoza testu zmieniły się w trakcie (inne programy?):"
            diff "$TMP/foreign-before.txt" "$TMP/foreign-after.txt" | sed 's/^/INFO:   /'
        fi
    fi
    rm -rf "$TMP"
    echo "=== PODSUMOWANIE: PASS=$PASSES FAIL=$FAILS"
    if [ "$FAILS" -gt 0 ] || [ "$rc" -ne 0 ]; then exit 1; fi
    exit 0
}
trap cleanup EXIT
trap 'echo "przerwano"; exit 130' INT TERM

abort() { fail "$*"; echo "Przerywam test (bezpieczeństwo)."; exit 1; }

# ---------------------------------------------------------------- start
COMMON='state.restore-props = false, state.restore-target = false'
start_app() { # start_app <nazwa> <kanały> [media.name]
    local name=$1 ch=$2 media=${3:-"dd-test playback"}
    local map=""
    [ "$ch" = 1 ] && map="--channel-map MONO"
    pw-cat --playback --raw --format s16 --rate 48000 --channels "$ch" $map --target "$SINK" \
        -P "{ node.name = \"$name\", application.name = \"$name\", media.name = \"$media\", $COMMON }" \
        /dev/zero >/dev/null 2>&1 &
    PID_OF[$name]=$!
}
start_capture() { # start_capture <klucz> <node.name> <target> [media.name]
    local key=$1 name=$2 target=$3 media=${4:-"dd-test capture"}
    pw-cat --record --raw --format s16 --rate 48000 --channels 2 --target "$target" \
        -P "{ node.name = \"$name\", application.name = \"$name\", application.process.binary = \"Discord\", media.name = \"$media\", node.dont-fallback = true, node.dont-reconnect = true, $COMMON }" \
        /dev/null >/dev/null 2>&1 &
    PID_OF[$key]=$!
}
new_id() { # new_id <node.name> <stare id...> -> id, którego nie było
    local name=$1; shift
    for i in $(q ids "$name"); do
        local seen=0
        for o in "$@"; do [ "$o" = "$i" ] && seen=1; done
        [ $seen = 0 ] && { echo "$i"; return; }
    done
}
link_monitor_to() { # link_monitor_to <node-id>
    local sink_id=$1 dst=$2
    pw-link "$(q port "$sink_id" monitor_FL)" "$(q port "$dst" input_FL)" >/dev/null
    pw-link "$(q port "$sink_id" monitor_FR)" "$(q port "$dst" input_FR)" >/dev/null
}
list_json() { "${TOOL[@]}" --list --json > "$TMP/list.json" 2>"$TMP/list.err"; }
safe_guard() {
    list_json || abort "--list --json nie działa: $(cat "$TMP/list.err")"
    q group-safe "$TMP/list.json" >/dev/null || abort "narzędzie widzi węzły spoza testu: $(q group-safe "$TMP/list.json")"
}
jq_py() { python3 -c "import json,sys; d=json.load(open('$TMP/list.json')); $1"; }

echo "=== Warpipe — test na żywo ($(date -Is))"
command -v pw-cat >/dev/null && command -v pw-link >/dev/null && command -v pactl >/dev/null || abort "brak pw-cat/pw-link/pactl"
[ -z "$(q leftovers)" ] || abort "w grafie są już obiekty dd-test (poprzedni test?): $(q leftovers)"

sha256sum "$WP_STATE_DIR"/* > "$TMP/wp-state.sha256" 2>/dev/null
DEF_SINK=$(pactl get-default-sink)
DEF_SOURCE=$(pactl get-default-source)
q foreign > "$TMP/foreign-before.txt"

cat > "$WARPIPE_CONFIG_DIR/config.json" <<'JSON'
{
  "version": 1,
  "selected_apps": [],
  "enforce": false,
  "capture_match_terms": ["dd-test-discord"],
  "capture_names": ["dd-test-discord_capture"],
  "mic_names": ["dd-test-mic", "recStream", "WEBRTC VoiceEngine"]
}
JSON

MOD=$(pactl load-module module-null-sink sink_name=$SINK \
    "sink_properties=state.restore-props=false state.restore-target=false device.description=dd-test-sink") \
    || abort "nie udało się utworzyć null sink"
q wait-node "$SINK" 1 5 || abort "null sink się nie pojawił"
SINK_ID=$(q ids "$SINK")
info "null sink #$SINK_ID (moduł $MOD)"

start_app dd-test-game 2
start_app dd-test-browser 2 "dd-test-PRIVATE-tytul-karty"
start_app dd-test-mono 1
for a in dd-test-game dd-test-browser dd-test-mono; do q wait-node "$a" 1 5 || abort "atrapa $a się nie pojawiła"; done
GAME=$(q ids dd-test-game); BROWSER=$(q ids dd-test-browser); MONO=$(q ids dd-test-mono)
info "aplikacje: game #$GAME [$(q ports "$GAME")], browser #$BROWSER, mono #$MONO [$(q ports "$MONO")]"

# atrapa mikrofonu rozmowy (recStream) — z połączeniami, które NIE mogą zostać ruszone
pw-cat --record --raw --format s16 --rate 48000 --channels 2 --target 0 \
    -P "{ node.name = \"dd-test-mic\", application.name = \"dd-test-discord-voice\", application.process.binary = \"Discord\", media.name = \"recStream\", $COMMON }" \
    /dev/null >/dev/null 2>&1 &
PID_OF[mic]=$!
q wait-node dd-test-mic 1 5 || abort "atrapa mikrofonu się nie pojawiła"
MIC=$(q ids dd-test-mic)
link_monitor_to "$SINK_ID" "$MIC"
MIC_LINKS=$(q inbound "$MIC")
[ -n "$MIC_LINKS" ] || abort "nie udało się podłączyć atrapy mikrofonu"
mic_untouched() { [ "$(q inbound "$MIC")" = "$MIC_LINKS" ]; }

# ================================================================ A: per aplikacja (jak oficjalny Discord)
echo "--- A: discord_capture na aplikację, połączenia robi WirePlumber"
start_capture capA1 dd-test-discord_capture "$(q serial "$GAME")" "dd-test game capture"
q wait-node dd-test-discord_capture 1 5 || abort "capA1 się nie pojawił"
A1=$(q ids dd-test-discord_capture)
start_capture capA2 dd-test-discord_capture "$(q serial "$BROWSER")" "dd-test game capture"
q wait-node dd-test-discord_capture 2 5 || abort "capA2 się nie pojawił"
A2=$(new_id dd-test-discord_capture "$A1")
q wait-inbound "$A1" "dd-test-game" 5 >/dev/null || abort "WirePlumber nie połączył game -> capA1"
q wait-inbound "$A2" "dd-test-browser" 5 >/dev/null || abort "WirePlumber nie połączył browser -> capA2"
info "capA1 #$A1 <- $(q inbound "$A1")"
info "capA2 #$A2 <- $(q inbound "$A2")"

safe_guard
check "A1 wykrycie: status found, grupa = oba discord_capture" \
    jq_py "assert d['status']=='found' and sorted(d['group'])==sorted([$A1,$A2]), d['group']"
check "A1 wykrycie: atrapa recStream rozpoznana jako mikrofon, nie w grupie" \
    jq_py "c=[c for c in d['candidates'] if c['id']==$MIC][0]; assert c['kind']=='mic' and $MIC not in d['group']"
check "A1 aplikacje: game/browser/mono na liście, bez atrap Discorda" \
    jq_py "k={a['key'] for a in d['apps']}; assert {'dd-test-game','dd-test-browser','dd-test-mono'}<=k and not any('discord' in x for x in k), k"

BEFORE_A1=$(q inbound "$A1"); BEFORE_A2=$(q inbound "$A2")
"${TOOL[@]}" --apply dd-test-game --dry-run > "$TMP/dry.out" 2>&1
check "A2 --dry-run: kod 0, plan pokazuje rozłączenie browser" grep -q "unlink.*dd-test-browser" "$TMP/dry.out"
check "A2 --dry-run: graf bez zmian" test "$(q inbound "$A1")|$(q inbound "$A2")" = "$BEFORE_A1|$BEFORE_A2"

safe_guard
"${TOOL[@]}" --apply dd-test-game > "$TMP/apply.out" 2>&1; rc=$?
check "A3 --apply dd-test-game: kod 0" test $rc -eq 0
check "A3 capA1 <- tylko game (FL->FL, FR->FR)" \
    test "$(q inbound "$A1")" = "dd-test-game#$GAME:output_FL>input_FL dd-test-game#$GAME:output_FR>input_FR"
check "A3 capA2 <- nic (browser odłączony)" test -z "$(q inbound "$A2")"
check "A3 mikrofon nietknięty" mic_untouched
check "A3 browser nadal gra do null sinka" test -n "$(q inbound "$SINK_ID" | grep -o dd-test-browser | head -1)"

# czy WirePlumber sam odtwarza usunięte połączenie przy rescanie? (informacyjnie)
pw-cat --playback --raw --format s16 --rate 48000 --channels 2 --target "$SINK" \
    -P "{ node.name = \"dd-test-blip\", application.name = \"dd-test-blip\", $COMMON }" /dev/zero >/dev/null 2>&1 &
PID_OF[blip]=$!
sleep 1.5
kill_pid "${PID_OF[blip]}"; unset 'PID_OF[blip]'
sleep 0.5
if [ -z "$(q inbound "$A2")" ]; then info "WirePlumber NIE odtworzył usuniętego połączenia po rescanie (nowy strumień)"
else info "WirePlumber odtworzył połączenie po rescanie: $(q inbound "$A2") — tryb --watch to poprawi"; fi
# przywróć stan po ewentualnym rescanie, żeby kolejne kroki były deterministyczne
"${TOOL[@]}" --apply dd-test-game >/dev/null 2>&1

safe_guard
"${TOOL[@]}" --apply dd-test-game dd-test-mono > "$TMP/apply2.out" 2>&1; rc=$?
check "A4 --apply game+mono: kod 0" test $rc -eq 0
check "A4 capA1 <- game + mono (mono bez własnego capture -> nośnik)" \
    test "$(q inbound-src "$A1")" = "dd-test-game dd-test-mono"
check "A4 mono podłączone do obu kanałów wejścia" \
    sh -c "test \"\$(python3 '$TMP/q.py' inbound $A1 | tr ' ' '\n' | grep -c dd-test-mono)\" -ge 2"
check "A4 capA2 <- nic" test -z "$(q inbound "$A2")"
check "A4 nasze połączenia oznaczone (managed > 0)" test "$(q managed)" -gt 0

"${TOOL[@]}" --restore > "$TMP/restore.out" 2>&1; rc=$?
check "A5 --restore: kod 0" test $rc -eq 0
check "A5 capA1 <- tylko game (jak na początku)" test "$(q inbound "$A1")" = "$BEFORE_A1"
check "A5 capA2 <- browser przywrócony" test "$(q inbound "$A2")" = "$BEFORE_A2"
check "A5 brak naszych połączeń" test "$(q managed)" -eq 0
check "A5 mikrofon nietknięty" mic_untouched

# Cykl „odznacz → zaznacz → Cały system → odznacz → Cały system”: oryginał odtworzony
# przy ponownym zaznaczeniu NIE może być oznaczony jako nasz (inaczej przepada).
"${TOOL[@]}" --apply dd-test-game >/dev/null 2>&1
"${TOOL[@]}" --apply dd-test-game dd-test-browser >/dev/null 2>&1
check "A6 zaznaczenie browser z powrotem: capA2 <- browser" test "$(q inbound "$A2")" = "$BEFORE_A2"
check "A6 odtworzony oryginał bez znacznika managed" test "$(q managed)" -eq 0
"${TOOL[@]}" --restore >/dev/null 2>&1
check "A6 --restore: capA2 <- browser, brak naszych" test "$(q inbound "$A2")" = "$BEFORE_A2" -a "$(q managed)" -eq 0
"${TOOL[@]}" --apply dd-test-game >/dev/null 2>&1
check "A6 ponowne odznaczenie: capA2 <- nic" test -z "$(q inbound "$A2")"
"${TOOL[@]}" --restore > "$TMP/restore2.out" 2>&1
check "A6 drugi cykl --restore: capA2 <- browser" test "$(q inbound "$A2")" = "$BEFORE_A2"
"${TOOL[@]}" --restore > "$TMP/restore3.out" 2>&1; rc=$?
check "A6 --restore drugi raz: bez zmian (0/0)" \
    sh -c "test $rc -eq 0 && grep -q 'restored 0, removed 0' '$TMP/restore3.out'"
check "A6 stan jak na początku: capA1/capA2, brak naszych" \
    test "$(q inbound "$A1")|$(q inbound "$A2")|$(q managed)" = "$BEFORE_A1|$BEFORE_A2|0"
info "odtworzone oryginały (znacznik restored): $(q restored)"

"${TOOL[@]}" --apply dd-test-gaem > "$TMP/typo.out" 2>&1; rc=$?
check "A7 --apply z literówką: kod 2, bez zmian, podpowiedź nazw" \
    sh -c "test $rc -eq 2 && grep -q dd-test-game '$TMP/typo.out' && test \"\$(python3 '$TMP/q.py' inbound $A1)|\$(python3 '$TMP/q.py' inbound $A2)\" = '$BEFORE_A1|$BEFORE_A2'"
check "A7 mikrofon nietknięty" mic_untouched

kill_pid "${PID_OF[capA1]}"; kill_pid "${PID_OF[capA2]}"; unset 'PID_OF[capA1]' 'PID_OF[capA2]'
q wait-gone "$A1" 5; q wait-gone "$A2" 5

# ================================================================ B: monitor wyjścia
echo "--- B: jeden strumień czyta monitor wyjścia (połączenia ręczne pw-link)"
start_capture capB dd-test-discord-generic 0 "RecordStream"
q wait-node dd-test-discord-generic 1 5 || abort "capB się nie pojawił"
B=$(q ids dd-test-discord-generic)
link_monitor_to "$SINK_ID" "$B"
BEFORE_B=$(q inbound "$B")
info "capB #$B <- $BEFORE_B"

safe_guard
# Sam monitor wyjścia to słaby dowód (tak samo wygląda mikrofon na monitorze):
# bez jednorazowego potwierdzenia narzędzie niczego nie rusza.
check "B1 wykrycie: niepewne, capB wymaga potwierdzenia (może to być mikrofon)" \
    jq_py "c=[c for c in d['candidates'] if c['id']==$B][0]; assert d['status']=='ambiguous' and d['group']==[] and c['needs_confirm'] and not c['confirmed'], d"
"${TOOL[@]}" --apply dd-test-browser > "$TMP/b0.out" 2>&1; rc=$?
check "B1 --apply bez potwierdzenia: kod 3, bez zmian" test $rc -eq 3 -a "$(q inbound "$B")" = "$BEFORE_B"
"${TOOL[@]}" --apply dd-test-browser --capture "$B" > "$TMP/b.out" 2>&1; rc=$?
check "B2 --apply dd-test-browser --capture B: kod 0" test $rc -eq 0
check "B2 potwierdzenie zapamiętane w konfiguracji" \
    python3 -c "import json,sys; c=json.load(open('$WARPIPE_CONFIG_DIR/config.json')); sys.exit(0 if {'program':'Discord','node':'dd-test-discord-generic','sink':'$SINK'} in c.get('confirmed_captures',[]) else 1)"
check "B2 capB <- tylko browser" \
    test "$(q inbound "$B")" = "dd-test-browser#$BROWSER:output_FL>input_FL dd-test-browser#$BROWSER:output_FR>input_FR"
check "B2 mikrofon nietknięty" mic_untouched
"${TOOL[@]}" --restore >/dev/null 2>&1; rc=$?
check "B3 --restore: capB <- monitor jak na początku" test $rc -eq 0 -a "$(q inbound "$B")" = "$BEFORE_B"
check "B3 brak naszych połączeń" test "$(q managed)" -eq 0
safe_guard
check "B3 po potwierdzeniu: capB wykrywany sam" jq_py "assert d['status']=='found' and d['group']==[$B], d"

echo "--- B': dwa podobne strumienie = niepewne wykrycie"
start_capture capB2 dd-test-discord-generic 0 "RecordStream"
q wait-node dd-test-discord-generic 2 5 || abort "capB2 się nie pojawił"
B2=$(new_id dd-test-discord-generic "$B")
link_monitor_to "$SINK_ID" "$B2"
BEFORE_B2=$(q inbound "$B2")
safe_guard
check "B4 wykrycie: ambiguous, bez grupy" jq_py "assert d['status']=='ambiguous' and d['group']==[], d['status']"
"${TOOL[@]}" --apply dd-test-browser > "$TMP/amb.out" 2>&1; rc=$?
check "B4 --apply przy niepewności: kod 3, bez zmian" \
    test $rc -eq 3 -a "$(q inbound "$B")|$(q inbound "$B2")" = "$BEFORE_B|$BEFORE_B2"
"${TOOL[@]}" --apply dd-test-browser --capture "$B2" > "$TMP/amb2.out" 2>&1; rc=$?
check "B5 --apply --capture B2: tylko B2 zmieniony" \
    test $rc -eq 0 -a "$(q inbound-src "$B2")" = "dd-test-browser" -a "$(q inbound "$B")" = "$BEFORE_B"
"${TOOL[@]}" --restore >/dev/null 2>&1
check "B6 --restore: B2 jak na początku" test "$(q inbound "$B2")" = "$BEFORE_B2"
kill_pid "${PID_OF[capB2]}"; unset 'PID_OF[capB2]'; q wait-gone "$B2" 5

# ================================================================ W: --watch
echo "--- W: pilnowanie (--watch)"
safe_guard
python3 "$HERE/bin/warpipe" --watch --apply dd-test-game > "$TMP/watch.log" 2>&1 &
WATCH_PID=$!
check "W1 --watch: capB <- tylko game w ciągu 3 s" q wait-inbound "$B" "dd-test-game" 3
link_monitor_to "$SINK_ID" "$B"   # „Discord” podłącza monitor ponownie
check "W2 ponowne podłączenie monitora usunięte w ciągu 3 s" q wait-inbound "$B" "dd-test-game" 3
check "W2 mikrofon nietknięty" mic_untouched

OLD_GAME=$GAME
OLD_GAME_SERIAL=$(q serial "$GAME")
kill_pid "${PID_OF[dd-test-game]}"
q wait-gone "$OLD_GAME" 5
start_app dd-test-game 2
q wait-node dd-test-game 1 5 || abort "nowa atrapa game się nie pojawiła"
GAME=$(q ids dd-test-game)
GAME_SERIAL=$(q serial "$GAME")
# id bywa użyte ponownie przez PipeWire — nowość sprawdzamy po object.serial
check "W3 restart aplikacji (serial $OLD_GAME_SERIAL -> $GAME_SERIAL) podłączony w ciągu 3 s" \
    sh -c "test '$GAME_SERIAL' != '$OLD_GAME_SERIAL' && python3 '$TMP/q.py' wait-inbound $B dd-test-game 3 && python3 '$TMP/q.py' inbound $B | grep -q 'dd-test-game#$GAME:'"

kill_pid "${PID_OF[capB]}"; unset 'PID_OF[capB]'; q wait-gone "$B" 5
start_capture capC dd-test-discord_capture "$(q serial "$BROWSER")" "dd-test game capture"
q wait-node dd-test-discord_capture 1 5 || abort "capC się nie pojawił"
C=$(q ids dd-test-discord_capture)
check "W4 nowy discord_capture (Go Live) -> automatycznie tylko game w ciągu 3 s" \
    q wait-inbound "$C" "dd-test-game" 3
safe_guard

# W6: Discord's notification "ding": a new short-lived stream, a new discord_capture for it,
# WirePlumber links them — the fast path must cut that link within tens of ms (no debounce).
q link-lifetime dd-test-ding dd-test-discord_capture 8 > "$TMP/ding.out" 2>&1 &
LT_PID=$!
sleep 0.7   # let the helper's pw-dump -m start
start_app dd-test-ding 2
q wait-node dd-test-ding 1 5 || abort "atrapa dd-test-ding się nie pojawiła"
DING=$(q ids dd-test-ding)
start_capture capD dd-test-discord_capture "$(q serial "$DING")" "dd-test ding capture"
wait "$LT_PID"
DING_MS=$(cat "$TMP/ding.out")
info "W6 ding: połączenie dd-test-ding -> discord_capture istniało ${DING_MS} ms"
if [ "$DING_MS" = unseen ]; then
    info "W6 połączenie dinga żyło za krótko, by pw-dump -m je pokazał"
    DING_MS=0
fi
check "W6 ding odcięty od razu (szybka ścieżka): < 150 ms (zmierzono: ${DING_MS} ms)" \
    sh -c "test '$DING_MS' != timeout && test '${DING_MS:-9999}' -lt 150"
D=$(new_id dd-test-discord_capture "$C")
check "W6 nowy discord_capture dinga bez połączeń, game dalej w C" \
    sh -c "test -n '$D' && python3 '$TMP/q.py' wait-inbound '$D' '' 2 && python3 '$TMP/q.py' wait-inbound '$C' dd-test-game 2"
grep -q "cut a new link to the stream right away" "$TMP/watch.log" && info "W6 log narzędzia: szybkie cięcie zapisane"
kill_pid "${PID_OF[capD]}"; unset 'PID_OF[capD]'
kill_pid "${PID_OF[dd-test-ding]}"; unset 'PID_OF[dd-test-ding]'
q wait-gone "$DING" 5
check "W6 mikrofon nietknięty" mic_untouched

kill -TERM "$WATCH_PID"
WRC=124
for _ in $(seq 1 50); do
    if ! kill -0 "$WATCH_PID" 2>/dev/null; then wait "$WATCH_PID"; WRC=$?; break; fi
    sleep 0.1
done
WATCH_PID=""
check "W5 SIGTERM: --watch kończy się z kodem 0" test "$WRC" -eq 0
check "W5 po wyjściu: capC <- browser (stan Discorda przywrócony)" q wait-inbound "$C" "dd-test-browser" 3
check "W5 brak naszych połączeń" test "$(q managed)" -eq 0
check "W5 mikrofon nietknięty" mic_untouched

# ================================================================ K: SIGKILL of the tool
echo "--- K: SIGKILL narzędzia — jego monitor pw-dump -m nie może zostać (PR_SET_PDEATHSIG)"
python3 "$HERE/bin/warpipe" --watch > "$TMP/watch-kill.log" 2>&1 &
KILL_PID=$!
MON_PID=""
for _ in $(seq 1 50); do
    MON_PID=$(pgrep -P "$KILL_PID" -f "pw-dump -m" | head -n1)   # only OUR tool's child
    [ -n "$MON_PID" ] && break
    sleep 0.1
done
check "K1 --watch uruchomił monitor pw-dump -m" test -n "$MON_PID"
kill -9 "$KILL_PID" 2>/dev/null
wait "$KILL_PID" 2>/dev/null
KILL_PID=""
MON_GONE=1
if [ -n "$MON_PID" ]; then
    MON_GONE=0
    for _ in $(seq 1 30); do
        if ! tr '\0' ' ' < "/proc/$MON_PID/cmdline" 2>/dev/null | grep -q "pw-dump"; then MON_GONE=1; break; fi
        sleep 0.1
    done
    [ "$MON_GONE" = 1 ] || kill "$MON_PID" 2>/dev/null   # never leave it behind
fi
check "K2 po SIGKILL narzędzia monitor pw-dump -m kończy się sam (w ciągu 3 s)" test "$MON_GONE" = 1
"${TOOL[@]}" --restore >/dev/null 2>&1
check "K3 brak naszych połączeń" test "$(q managed)" -eq 0

# ================================================================ S: stara migawka (np. po restarcie PipeWire)
echo "--- S: migawka z innego uruchomienia PipeWire / niezgodna z grafem"
# Własny, niepodłączony strumień nagrywania (cel 0 = WirePlumber go nie łączy).
pw-cat --record --raw --format s16 --rate 48000 --channels 2 --target 0 \
    -P "{ node.name = \"dd-test-stale\", application.name = \"dd-test-stale\", media.name = \"dd-test stale\", node.dont-reconnect = true, $COMMON }" \
    /dev/null >/dev/null 2>&1 &
PID_OF[stale]=$!
q wait-node dd-test-stale 1 5 || abort "dd-test-stale się nie pojawił"
STALE=$(q ids dd-test-stale)
SNAP="$WARPIPE_RUNTIME_DIR/snapshot.json"
INST=$(q instance)
check "S0 dd-test-stale bez połączeń" test -z "$(q inbound "$STALE")"
q write-snapshot "$SNAP" "0:0" "$STALE" "$BROWSER" output_FL input_FL
"${TOOL[@]}" --restore > "$TMP/s1.out" 2>&1; rc=$?
check "S1 migawka z innego uruchomienia PipeWire: nic nie połączone, migawka usunięta" \
    test $rc -eq 0 -a -z "$(q inbound "$STALE")" -a ! -e "$SNAP"
q write-snapshot "$SNAP" "$INST" "$STALE" "$BROWSER" output_FL input_FL out_port_name=output_FR
"${TOOL[@]}" --restore > "$TMP/s2.out" 2>&1; rc=$?
check "S2 ta sama instancja, ale nazwa portu się nie zgadza: nic nie połączone" \
    test $rc -eq 0 -a -z "$(q inbound "$STALE")"
q write-snapshot "$SNAP" "$INST" "$STALE" "$BROWSER" output_FL input_FL
"${TOOL[@]}" --restore > "$TMP/s3.out" 2>&1; rc=$?
check "S3 kontrola: poprawna migawka odtwarza połączenie (bez znacznika managed)" \
    test $rc -eq 0 -a "$(q inbound "$STALE")" = "dd-test-browser#$BROWSER:output_FL>input_FL" -a "$(q managed)" -eq 0
check "S mikrofon nietknięty" mic_untouched
kill_pid "${PID_OF[stale]}"; unset 'PID_OF[stale]'; q wait-gone "$STALE" 5

# ================================================================ D: zrzut diagnostyczny
"${TOOL[@]}" --dump "$TMP/dump.json" >/dev/null 2>&1; rc=$?
check "D1 --dump: kod 0, poprawny JSON" sh -c "test $rc -eq 0 && python3 -c 'import json; json.load(open(\"$TMP/dump.json\"))'"
check "D1 --dump bez nazwy użytkownika i hosta" \
    sh -c "! grep -q -e '$(id -un)' -e '$(hostname)' '$TMP/dump.json'"
check "D1 --dump bez media.name (tytułu)" sh -c "! grep -q PRIVATE '$TMP/dump.json'"
rm -f "$TMP/dump.json"

# ================================================================ prywatność
if grep -rq "PRIVATE" "$TMP/watch.log" "$TMP"/*.out "$WARPIPE_CONFIG_DIR" "$WARPIPE_RUNTIME_DIR" 2>/dev/null; then
    fail "P1 media.name przeciekł do logów/plików"
else
    pass "P1 media.name (tytuł) nie trafił do logów, konfiguracji ani migawki"
fi
info "log --watch (ostatnie linie):"
tail -n 8 "$TMP/watch.log" | sed 's/^/INFO:   /'
