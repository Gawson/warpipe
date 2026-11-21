#!/bin/bash
# Live test: OpenDeck plugin <-> tool (--watch) <-> PipeWire.
#
# Rules (strict, as in live_pipewire.sh):
#  - only OUR OWN objects with the ddp-test prefix: a null sink (pactl load-module), fake apps
#    (pw-cat playing SILENCE from /dev/zero), fake Discord streams (pw-cat --record to /dev/null);
#  - the tool gets a test config: "Discord" = only the ddp-test-discord* nodes;
#  - OpenDeck is NOT needed: tests/fake_opendeck.py starts the plugin the way OpenDeck does;
#  - cleanup in trap EXIT (also on error): processes from <tmp>/pids, the null sink module;
#    then pw-dump verifies that nothing is left and that the WirePlumber state did not change.
#
# Output: PASS:/FAIL:/INFO: lines and a summary; exit code 0 = everything passed.

set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
BASE_TMP=${WARPIPE_TEST_TMP:-${XDG_RUNTIME_DIR:-/tmp}}
mkdir -p "$BASE_TMP"
TMP=$(mktemp -d "$BASE_TMP/ddp-live.XXXXXX")
export WARPIPE_CONFIG_DIR="$TMP/config"
export WARPIPE_RUNTIME_DIR="$TMP/run"
export WARPIPE_LANG=en   # the checks below expect English texts (tool and plugin)
mkdir -p "$WARPIPE_CONFIG_DIR" "$WARPIPE_RUNTIME_DIR"
SINK=ddp-test-sink
WP_STATE_DIR="$HOME/.local/state/wireplumber"
PASSES=0
FAILS=0
SCEN_RC=0

pass() { echo "PASS: $*"; PASSES=$((PASSES + 1)); }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
info() { echo "INFO: $*"; }

# ---------------------------------------------------------------- pomocnik (tylko odczyt pw-dump)
cat > "$TMP/q.py" <<'PY'
import json, subprocess, sys
d = json.loads(subprocess.run(["pw-dump", "-N"], capture_output=True, text=True, check=True).stdout)
nodes = {o["id"]: o for o in d if o.get("type") == "PipeWire:Interface:Node" and o.get("info")}
ports = {o["id"]: o for o in d if o.get("type") == "PipeWire:Interface:Port" and o.get("info")}
links = [o for o in d if o.get("type") == "PipeWire:Interface:Link" and o.get("info")]
P = lambda o: o["info"].get("props") or {}
cmd = sys.argv[1]
if cmd == "leftovers":
    bad = [f'node#{i}:{P(n).get("node.name")}' for i, n in nodes.items() if "ddp-test" in json.dumps(P(n))]
    bad += [f'link#{l["id"]}' for l in links if "warpipe.managed" in P(l) or "warpipe.restored" in P(l)]
    print(" ".join(bad))
elif cmd == "foreign":
    test = {i for i, n in nodes.items() if "ddp-test" in json.dumps(P(n))}
    def nn(i): return P(nodes[i]).get("node.name") if i in nodes else "?"
    def pn(i): return P(ports[i]).get("port.name") if i in ports else "?"
    out = []
    for l in links:
        i = l["info"]
        if i["output-node-id"] in test or i["input-node-id"] in test:
            continue
        out.append(f'{nn(i["output-node-id"])}:{pn(i["output-port-id"])}>{nn(i["input-node-id"])}:{pn(i["input-port-id"])}')
    print("\n".join(sorted(out)))
PY
q() { python3 -I "$TMP/q.py" "$@"; }

# ---------------------------------------------------------------- sprzątanie
proc_matches() { # proc_matches <pid> <wzorzec w cmdline>
    [ -r "/proc/$1/cmdline" ] && tr '\0' ' ' < "/proc/$1/cmdline" | grep -q -- "$2"
}
kill_pid() { # kill_pid <pid> <wzorzec> — tylko jeśli to nadal ten proces
    local p=$1 pat=$2
    proc_matches "$p" "$pat" || return 0
    kill -TERM "$p" 2>/dev/null
    for _ in $(seq 1 40); do proc_matches "$p" "$pat" || return 0; sleep 0.1; done
    kill -9 "$p" 2>/dev/null
}

cleanup() {
    local rc=$?
    set +e
    trap - EXIT INT TERM
    echo "--- sprzątanie"
    if [ -f "$TMP/pids" ]; then
        # najpierw narzędzia (przywracają połączenia), potem wtyczka, na końcu atrapy pw-cat
        while read -r pid name; do
            case "$name" in watch|relaunched-watch) kill_pid "$pid" "warpipe" ;; esac
        done < "$TMP/pids"
        while read -r pid name; do
            [ "$name" = plugin ] && kill_pid "$pid" "warpipe-plugin"
        done < "$TMP/pids"
        while read -r pid name; do
            case "$name" in watch|relaunched-watch|plugin) ;; *) kill_pid "$pid" "pw-cat" ;; esac
        done < "$TMP/pids"
    fi
    if [ -f "$TMP/module" ]; then pactl unload-module "$(cat "$TMP/module")" 2>/dev/null; fi
    for m in $(pactl list short modules 2>/dev/null | awk -v s="sink_name=$SINK" 'index($0, s) {print $1}'); do
        pactl unload-module "$m" 2>/dev/null
    done
    sleep 0.5
    local left
    left=$(q leftovers 2>/dev/null)
    if [ -z "$left" ]; then pass "sprzątanie: w pw-dump brak węzłów ddp-test i połączeń narzędzia"
    else fail "sprzątanie: zostało: $left"; fi
    if pactl list short modules | grep -q "sink_name=$SINK"; then fail "sprzątanie: moduł null-sink został"
    else pass "sprzątanie: brak modułu $SINK"; fi
    # Only the PIDs THIS run started (<tmp>/pids): a machine-wide pgrep would also see
    # other instances (a parallel unit-test run, OpenDeck running the installed plugin).
    local cats="" plugins=""
    if [ -f "$TMP/pids" ]; then
        while read -r pid name; do
            case "$name" in
                watch|relaunched-watch) ;;
                plugin) proc_matches "$pid" "warpipe-plugin" && plugins="$plugins $pid" ;;
                *) proc_matches "$pid" "pw-cat" && cats="$cats $pid" ;;
            esac
        done < "$TMP/pids"
    fi
    if [ -n "$cats" ]; then fail "sprzątanie: działa jeszcze pw-cat z testu:$cats"
    else pass "sprzątanie: brak procesów pw-cat z testu"; fi
    if [ -n "$plugins" ]; then fail "sprzątanie: działa jeszcze wtyczka z testu:$plugins"
    else pass "sprzątanie: brak procesu wtyczki z testu"; fi
    local alive=""
    if [ -f "$TMP/pids" ]; then
        while read -r pid name; do
            case "$name" in watch|relaunched-watch) proc_matches "$pid" "warpipe" && alive="$alive $pid" ;; esac
        done < "$TMP/pids"
    fi
    if [ -z "$alive" ]; then pass "sprzątanie: żadne narzędzie z testu nie działa (--watch, uruchomione przez wtyczkę)"
    else fail "sprzątanie: działa jeszcze narzędzie:$alive"; fi
    if [ -e "$WARPIPE_RUNTIME_DIR/control.sock" ]; then fail "sprzątanie: zostało gniazdo sterowania"
    else pass "sprzątanie: gniazdo sterowania usunięte"; fi
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
    if [ -s "$TMP/failures.log" ]; then
        info "szczegóły błędów:"
        sed 's/^/INFO:   /' "$TMP/failures.log" | head -60
    fi
    if [ "$FAILS" -gt 0 ] || [ "$SCEN_RC" -ne 0 ]; then
        info "log narzędzia (ostatnie linie):"
        tail -n 15 "$TMP/watch.log" 2>/dev/null | sed 's/^/INFO:   /'
    fi
    if [ -n "${DDP_KEEP_LOGS:-}" ]; then
        mkdir -p "$DDP_KEEP_LOGS" && cp "$TMP"/*.log "$TMP"/*.out "$DDP_KEEP_LOGS"/ 2>/dev/null
        info "logi skopiowane do $DDP_KEEP_LOGS"
    fi
    rm -rf "$TMP"
    echo "=== PODSUMOWANIE: PASS=$PASSES FAIL=$FAILS"
    if [ "$FAILS" -gt 0 ] || [ "$SCEN_RC" -ne 0 ] || [ "$rc" -ne 0 ]; then exit 1; fi
    exit 0
}
trap cleanup EXIT
trap 'echo "przerwano"; exit 130' INT TERM

echo "=== Warpipe — wtyczka OpenDeck, test na żywo ($(date -Is))"
for c in pw-cat pw-link pw-dump pactl; do
    command -v "$c" >/dev/null || { fail "brak $c"; exit 1; }
done
left=$(q leftovers)
[ -z "$left" ] || { fail "w grafie są już obiekty ddp-test (poprzedni test?): $left"; exit 1; }
sha256sum "$WP_STATE_DIR"/* > "$TMP/wp-state.sha256" 2>/dev/null
DEF_SINK=$(pactl get-default-sink)
DEF_SOURCE=$(pactl get-default-source)
q foreign > "$TMP/foreign-before.txt"

python3 -I "$HERE/tests/live_plugin_scenario.py" "$TMP" | tee "$TMP/scenario.out"
SCEN_RC=${PIPESTATUS[0]}
# grep -c kończy się kodem 1, gdy nic nie znalazł — to nie błąd testu
N_PASS=$(grep -c '^PASS:' "$TMP/scenario.out" || true)
N_FAIL=$(grep -c '^FAIL:' "$TMP/scenario.out" || true)
PASSES=$((PASSES + N_PASS))
FAILS=$((FAILS + N_FAIL))
