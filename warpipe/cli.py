"""Interfejs wiersza poleceń: warpipe."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from . import APP_NAME, __version__
from .control import ControlError, NotRunning
from .control import request as control_request
from .control import start_control_server
from .enforcer import Enforcer, Watcher
from .graph import Graph
from .planner import (KIND_LABEL_PL, STATUS_FOUND, STATUS_LABEL_PL, Detection,
                      app_key, detect, list_apps, plan_apply, plan_restore,
                      snapshot_originals)
from .pw import PwCli, PwError
from .scrub import scrub_dump
from .state import Config, InstanceLock, SnapshotStore

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_CAPTURE = 3
EXIT_LOCKED = 4
EXIT_NOT_RUNNING = 5

log = logging.getLogger("warpipe")

CAPTURE_LABEL_PL = {
    "active": "Stream z dźwiękiem: wykryty",
    "none": "Stream z dźwiękiem: nie wykryty",
    "ambiguous": "Stream z dźwiękiem: niepewny — wybierz w menu ikony",
}


def setup_logging(verbose: bool, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s warpipe %(levelname)s %(message)s", stream=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="warpipe",
        description=f"{APP_NAME}: do dźwięku streamu w Discordzie (Go Live) trafiają tylko wybrane aplikacje.",
        epilog="Bez argumentów (w sesji graficznej) uruchamia ikonę w zasobniku.",
    )
    ap.add_argument("--list", action="store_true", help="pokaż strumienie Discorda i aplikacje z dźwiękiem")
    ap.add_argument("--json", action="store_true", help="(z --list albo --status) wynik w formacie JSON")
    ap.add_argument("--status", action="store_true",
                    help="stan DZIAŁAJĄCEJ instancji (ikona albo --watch): tryb, stream, aplikacje")
    ap.add_argument("--toggle", metavar="APP",
                    help="w działającej instancji: dodaj/zabierz dźwięk aplikacji ze streamu "
                         "(jak przycisk w OpenDeck)")
    ap.add_argument("--system", action="store_true",
                    help="w działającej instancji: „Cały system” (przywróć zwykłe zachowanie Discorda)")
    ap.add_argument("--apply", nargs="+", metavar="APP",
                    help="jednorazowo: do dźwięku streamu tylko te aplikacje (nazwy jak w --list)")
    ap.add_argument("--capture", type=int, action="append", metavar="ID",
                    help="id węzła przechwytującego Discorda (z --list), gdy wykrywanie jest niepewne; można podać kilka razy")
    ap.add_argument("--restore", action="store_true", help="przywróć połączenia sprzed zmian (cały system)")
    ap.add_argument("--watch", action="store_true",
                    help="pilnuj bez ikony (aplikacje z --apply albo z konfiguracji); Ctrl+C przywraca")
    ap.add_argument("--dry-run", action="store_true", help="tylko pokaż, co zostałoby zmienione")
    ap.add_argument("--dump", metavar="PLIK", help="zapisz diagnostyczny pw-dump bez prywatnych danych ('-' = stdout)")
    ap.add_argument("--tray", action="store_true", help="ikona w zasobniku (domyślnie, gdy brak argumentów)")
    ap.add_argument("-v", "--verbose", action="store_true", help="więcej komunikatów")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


# --------------------------------------------------------------------------
def _ids_to_serials(graph: Graph, ids: list[int] | None) -> tuple[list[int] | None, str | None]:
    if not ids:
        return None, None
    out = []
    for i in ids:
        n = graph.nodes.get(i)
        if n is None:
            return None, f"Nie ma węzła o id {i} (sprawdź --list)."
        out.append(n.serial)
    return out, None


def _resolve_app_names(graph: Graph, cfg, names: list[str]) -> tuple[list[str], list[str]]:
    apps = list_apps(graph, cfg)
    by_cf = {a.key.casefold(): a.key for a in apps}
    keys, missing = [], []
    for n in names:
        k = by_cf.get(n.casefold())
        if k is None:
            missing.append(n)
            keys.append(n)
        else:
            keys.append(k)
    return keys, missing


def _candidate_json(graph: Graph, c, cfg) -> dict:
    inbound = []
    for l in graph.links_into(c.id):
        src = graph.nodes.get(l.out_node)
        sp = graph.ports.get(l.out_port)
        dp = graph.ports.get(l.in_port)
        inbound.append({
            "link_id": l.id,
            "from_node_id": l.out_node,
            "from_node": src.name if src else None,
            "from_class": src.media_class if src else None,
            "from_port": sp.name if sp else None,
            "to_port": dp.name if dp else None,
            "managed": l.managed,
        })
    return {
        "id": c.id, "serial": c.serial, "node_name": c.node.name,
        "binary": c.node.binary, "application_name": c.node.app_name,
        "kind": c.kind, "strong": c.strong, "reasons": c.reasons,
        "needs_confirm": c.needs_confirm, "confirmed": c.confirmed_by(cfg),
        "own_app": app_key(c.own_app) if c.own_app else None,
        "own_app_id": c.own_app.id if c.own_app else None,
        "mic_linked": c.mic_linked, "muted": c.muted,
        "in_ports": [p.name for p in c.node.in_ports],
        "inbound": inbound,
    }


def cmd_list(pw, config: Config, args) -> int:
    graph = Graph.from_dump(pw.dump())
    cfg = config.detect_config()
    override, err = _ids_to_serials(graph, args.capture)
    if err:
        print(err, file=sys.stderr)
        return EXIT_USAGE
    det = detect(graph, cfg, override)
    apps = list_apps(graph, cfg)
    group_ids = {c.id for c in det.group}
    feeding = {}
    for l in graph.links.values():
        if l.in_node in group_ids:
            feeding.setdefault(l.out_node, set()).add(l.in_node)

    if args.json:
        out = {
            "status": det.status,
            "status_text": STATUS_LABEL_PL.get(det.status, det.status),
            "message": det.message,
            "warnings": det.warnings,
            "group": [c.id for c in det.group],
            "candidates": [_candidate_json(graph, c, cfg) for c in det.candidates],
            "apps": [{
                "key": a.key, "display": a.display,
                "node_ids": [n.id for n in a.nodes],
                "has_pid": a.has_pid,
                "feeds_capture": sorted({cid for n in a.nodes for cid in feeding.get(n.id, ())}),
            } for a in apps],
            "config": {"selected_apps": config.selected_apps, "enforce": config.enforce,
                       "path": str(config.path)},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(STATUS_LABEL_PL.get(det.status, det.status) + (f" ({det.message})" if det.message else ""))
    for w in det.warnings:
        print("  " + w)
    print()
    print("Strumienie nagrywania Discorda:")
    if not det.candidates:
        print("  (brak)")
    for c in det.candidates:
        mark = "*" if c.id in group_ids else " "
        print(f" {mark} {c.summary()}")
        print(f"      serial {c.serial}; powody: {', '.join(c.reasons)}")
        if c.needs_confirm and not c.confirmed_by(cfg):
            print(f"      tylko po monitorze wyjścia (może to być mikrofon) — potwierdź: --capture {c.id}")
        for l in graph.links_into(c.id):
            src = graph.nodes.get(l.out_node)
            sp = graph.ports.get(l.out_port)
            dp = graph.ports.get(l.in_port)
            tag = " [nasze]" if l.managed else ""
            print(f"      <- {(src.name if src else '?')}#{l.out_node}:{sp.name if sp else '?'} -> {dp.name if dp else '?'}{tag}")
    print()
    print("Aplikacje z dźwiękiem (nazwa do --apply):")
    if not apps:
        print("  (brak)")
    sel = {k.casefold() for k in config.selected_apps}
    for a in apps:
        box = "[x]" if a.key.casefold() in sel else "[ ]"
        ids = ",".join(f"#{n.id}" for n in a.nodes)
        extra = []
        if not a.has_pid:
            extra.append("bez PID — Discord sam tego nie łapie")
        if any(n.id in feeding for n in a.nodes):
            extra.append("idzie do Discorda")
        print(f"  {box} {a.display}  ({ids})" + (f"  — {'; '.join(extra)}" if extra else ""))
    print()
    print(f"Konfiguracja: {config.path}")
    print(f"  wybrane: {', '.join(config.selected_apps) or '(nic)'}; pilnowanie przy starcie: {'tak' if config.enforce else 'nie'}")
    return EXIT_OK


def _print_ops(plan) -> None:
    if not plan.ops:
        print("Nic do zmiany.")
    for op in plan.ops:
        verb = "połącz  " if op.kind == "link" else "rozłącz "
        print(f"  {verb} {op.desc}")
    for n in plan.notes:
        print(f"  (uwaga: {n})")


def cmd_apply(pw, config: Config, args) -> int:
    graph = Graph.from_dump(pw.dump())
    cfg = config.detect_config()
    override, err = _ids_to_serials(graph, args.capture)
    if err:
        print(err, file=sys.stderr)
        return EXIT_USAGE
    keys, missing = _resolve_app_names(graph, cfg, args.apply)
    if missing and len(missing) == len(args.apply):
        # Literówka nie może wyciszyć całego streamu: bez żadnej znanej aplikacji nic nie zmieniamy.
        names = ", ".join(f"„{m}”" for m in missing)
        print(f"Nie widzę teraz żadnej z podanych aplikacji ({names}) — nic nie zmieniam.", file=sys.stderr)
        apps = list_apps(graph, cfg)
        if apps:
            print("Aplikacje, które teraz grają (nazwa do --apply): "
                  + ", ".join(a.key for a in apps), file=sys.stderr)
        else:
            print("Żadna aplikacja teraz nie gra.", file=sys.stderr)
        return EXIT_USAGE
    for m in missing:
        print(f"Uwaga: nie widzę teraz aplikacji „{m}” — ona NIE pójdzie do Discorda "
              f"(sprawdź nazwę w --list).", file=sys.stderr)
    store = SnapshotStore()
    enf = Enforcer(pw, config, store, enabled=True, selected=keys, persist=False, dcfg=cfg)
    det = detect(graph, cfg, override, enf._known(graph))
    if not det.found:
        print(f"{STATUS_LABEL_PL.get(det.status, det.status)}: {det.message}", file=sys.stderr)
        _print_candidates_hint(det)
        return EXIT_NO_CAPTURE
    plan = plan_apply(graph, det.group_nodes, keys, cfg, snapshot_originals(graph, store.data))
    print(f"Strumień Discorda: {', '.join(c.summary() for c in det.group)}")
    _print_ops(plan)
    if args.dry_run:
        return EXIT_OK
    # --capture = wybór użytkownika; potwierdzenie strumienia „z monitora” zostaje zapamiętane
    st = enf.set_capture_override(override) if override else enf.refresh("--apply")
    if st.error:
        print(f"Błąd: {st.error}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Gotowe: {st.last_action or 'bez zmian'}. Cofnięcie: warpipe --restore")
    return EXIT_OK


def _print_candidates_hint(det: Detection) -> None:
    if det.candidates:
        print("Kandydaci (podaj --capture ID):", file=sys.stderr)
        for c in det.candidates:
            print(f"  {c.summary()} — {', '.join(c.reasons)}", file=sys.stderr)


def cmd_restore(pw, config: Config, args) -> int:
    store = SnapshotStore()
    if args.dry_run:
        graph = Graph.from_dump(pw.dump())
        _print_ops(plan_restore(graph, store.data))
        return EXIT_OK
    enf = Enforcer(pw, config, store, enabled=False, persist=False)
    ok, msg = enf.restore("--restore")
    if not ok:
        print(f"Błąd przywracania: {msg}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Przywrócono ({msg}).")
    return EXIT_OK


def cmd_watch(pw, config: Config, args) -> int:
    cfg = config.detect_config()
    if args.apply:
        # --watch --apply: na sztywno te aplikacje; zmiany przez kanał sterowania nie są zapisywane
        keys, enabled, persist = list(args.apply), True, False
    else:
        # --watch bez --apply: dokładnie jak ikona — wybór i tryb z konfiguracji, zmiany
        # (np. z wtyczki OpenDeck) są zapisywane
        keys, enabled, persist = config.selected_apps, config.enforce, True
    if enabled and not keys:
        print("Uwaga: nie wybrano żadnej aplikacji — do Discorda nie pójdzie nic.", file=sys.stderr)
    enf = Enforcer(pw, config, SnapshotStore(), enabled=enabled, selected=keys, persist=persist, dcfg=cfg)
    if args.capture:
        graph = Graph.from_dump(pw.dump())
        override, err = _ids_to_serials(graph, args.capture)
        if err:
            print(err, file=sys.stderr)
            return EXIT_USAGE
        # tylko ustawienie + zapamiętanie potwierdzenia; pierwsze odświeżenie zrobi Watcher
        # (już z obsługą SIGTERM/SIGINT, która przywraca stan)
        enf.override = override
        enf._remember_confirmation(override)
    watcher = Watcher(enf, pw)

    def on_signal(signum, frame):
        log.info("sygnał %s — kończę i przywracam", signum)
        watcher.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    if enabled:
        log.info("pilnuję: %s", ", ".join(keys) or "(nic)")
    else:
        log.info("tryb „Cały system” (pilnowanie wyłączone w konfiguracji); czekam na polecenia")
    server = start_control_server(enf)
    try:
        watcher.run()
    finally:
        if server is not None:
            server.stop()
        ok, msg = enf.shutdown()
        if not ok:
            print(f"Błąd przywracania przy wyjściu: {msg}", file=sys.stderr)
    return EXIT_OK


# --------------------------------------------------------------------------
# Polecenia do działającej instancji (kanał sterowania)
# --------------------------------------------------------------------------

def _print_status(status: dict) -> None:
    cap = status.get("capture") or {}
    label = CAPTURE_LABEL_PL.get(cap.get("status"), "Stream z dźwiękiem: ?")
    n = cap.get("count") or 0
    print(label + (f" ({n} strumienie)" if n > 1 else ""))
    apps = status.get("apps") or []
    if status.get("enforce"):
        chosen = [a["key"] for a in apps if a.get("selected")]
        print("Do Discorda tylko: " + (", ".join(chosen) if chosen else "nic (nic nie zaznaczono)"))
    else:
        print("Do Discorda: cały dźwięk systemu")
    if status.get("error"):
        print("Błąd: " + str(status["error"]))
    print()
    print("Aplikacje:")
    if not apps:
        print("  (żadna aplikacja teraz nie gra)")
    for a in apps:
        box = "[x]" if a.get("selected") and status.get("enforce") else "[ ]"
        extra = []
        if not a.get("present"):
            extra.append("teraz nie gra")
        if a.get("in_stream"):
            extra.append("na streamie")
        print(f"  {box} {a.get('display') or a.get('key')}" + (f"  — {', '.join(extra)}" if extra else ""))


def cmd_remote(args) -> int:
    if args.status:
        req = {"cmd": "status"}
    elif args.system:
        req = {"cmd": "system"}
    else:
        req = {"cmd": "toggle", "app": args.toggle}
    try:
        reply = control_request(req)
    except NotRunning as e:
        print(f"{e} Uruchom go: warpipe (ikona) albo warpipe --watch.", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except ControlError as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return EXIT_ERROR
    if not reply.get("ok"):
        print(f"Błąd: {reply.get('error') or 'nieznany'}", file=sys.stderr)
        if args.json and isinstance(reply.get("status"), dict):
            print(json.dumps(reply["status"], ensure_ascii=False, indent=2))
        return EXIT_USAGE if "status" not in reply else EXIT_ERROR
    status = reply.get("status") if isinstance(reply.get("status"), dict) else {}
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return EXIT_OK
    if reply.get("message"):
        print(reply["message"])
        print()
    _print_status(status)
    return EXIT_OK


def cmd_dump(pw, args) -> int:
    data = scrub_dump(pw.dump())
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.dump == "-":
        sys.stdout.write(text)
    else:
        with open(args.dump, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Zapisano {args.dump} (bez tytułów strumieni i danych osobistych).")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    modes = [m for m in ("list", "restore", "watch", "dump", "tray", "status", "system") if getattr(args, m)]
    if args.toggle is not None:
        modes.append("toggle")
    if args.apply and not args.watch:
        modes.append("apply")
    if len(modes) > 1:
        ap.error("wybierz jedno: --list, --apply, --restore, --watch, --dump, --tray, --status, "
                 "--toggle, --system (wyjątek: --watch --apply)")
    if args.json and not (args.list or args.status):
        ap.error("--json działa tylko z --list albo --status")
    if args.dry_run and not (args.apply or args.restore) or (args.dry_run and args.watch):
        ap.error("--dry-run działa z --apply albo --restore")
    if modes and modes[0] in ("status", "toggle", "system"):
        if args.toggle is not None and not args.toggle.strip():
            ap.error("--toggle wymaga nazwy aplikacji (jak w --list)")
        # Tylko rozmowa z działającą instancją — bez blokady, bez PipeWire, bez konfiguracji.
        return cmd_remote(args)
    if not modes:
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            modes = ["tray"]
        else:
            ap.print_help()
            return EXIT_USAGE
    mode = modes[0]

    setup_logging(args.verbose, quiet=mode in ("list", "dump") or args.dry_run)
    config = Config()
    if config.load_error:
        print(f"Uwaga: {config.load_error} — używam ustawień domyślnych.", file=sys.stderr)
    pw = PwCli()

    if mode == "tray":
        from .tray import run_tray  # PySide6 tylko gdy potrzebne
        return run_tray(args, config, pw)

    try:
        if mode == "list":
            return cmd_list(pw, config, args)
        if mode == "dump":
            return cmd_dump(pw, args)
        needs_lock = not args.dry_run
        lock = InstanceLock()
        if needs_lock and not lock.acquire():
            pid = lock.holder_pid()
            print(f"Działa już inna instancja ({'PID ' + str(pid) if pid else 'ikona albo --watch'}). "
                  "Zamknij ją albo użyj jej menu.", file=sys.stderr)
            return EXIT_LOCKED
        try:
            if mode == "apply":
                return cmd_apply(pw, config, args)
            if mode == "restore":
                return cmd_restore(pw, config, args)
            if mode == "watch":
                return cmd_watch(pw, config, args)
        finally:
            lock.release()
    except PwError as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_USAGE
