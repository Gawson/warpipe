"""Command-line interface: warpipe."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import sys

from . import APP_NAME, __version__
from .control import ControlError, NotRunning, discord_sounds_text
from .control import request as control_request
from .control import start_control_server
from .enforcer import Enforcer, Watcher, routing_text
from .graph import Graph
from .i18n import N_, _, ngettext
from .planner import (STATUS_UNKNOWN_LABEL, Detection, app_key, detect,
                      is_discord_sound_stream, list_apps, plan_apply,
                      plan_restore, snapshot_originals, status_label)
from .pw import PwCli, PwError
from .scrub import scrub_dump
from .state import Config, InstanceLock, SnapshotStore, is_utf8_text

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_CAPTURE = 3
EXIT_LOCKED = 4
EXIT_NOT_RUNNING = 5

log = logging.getLogger("warpipe")

# Capture state as reported by a running instance (control.capture_state).
CAPTURE_LABEL = {
    "active": N_("Discord Go Live audio: active"),
    "none": N_("Discord Go Live audio: not detected"),
    "ambiguous": N_("Discord Go Live audio: ambiguous — pick one in the tray menu"),
}


def setup_logging(verbose: bool, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s warpipe %(levelname)s %(message)s", stream=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="warpipe",
        description=_("{app}: only the apps you pick are heard in your Discord Go Live stream.")
        .format(app=APP_NAME),
        epilog=_("Without arguments (in a graphical session) it starts the tray icon."),
    )
    ap.add_argument("--list", action="store_true", help=_("show Discord capture streams and apps playing audio"))
    ap.add_argument("--json", action="store_true", help=_("(with --list or --status) output JSON"))
    ap.add_argument("--status", action="store_true",
                    help=_("state of the RUNNING instance (tray icon or --watch): mode, stream, apps"))
    ap.add_argument("--toggle", metavar="APP",
                    help=_("in the running instance: add/remove an app's audio to/from the stream "
                           "(like the OpenDeck key)"))
    ap.add_argument("--force", action="store_true",
                    help=_("with --toggle: select the app even if it is not playing right now"))
    ap.add_argument("--system", action="store_true",
                    help=_("in the running instance: “Whole system” (restore Discord's normal behaviour)"))
    ap.add_argument("--discord-sounds", choices=("on", "off"), metavar="on|off",
                    help=_("Discord's own sounds (notification dings) on the stream: off = kept off "
                           "(the default, also in “Whole system” mode), on = let them through; saved"))
    ap.add_argument("--apply", nargs="+", metavar="APP",
                    help=_("one-shot: only these apps go to the stream audio (names as in --list)"))
    ap.add_argument("--capture", type=int, action="append", metavar="ID",
                    help=_("id of Discord's capture node (from --list) when detection is ambiguous; "
                           "may be given more than once"))
    ap.add_argument("--restore", action="store_true",
                    help=_("restore the links from before the changes (whole system)"))
    ap.add_argument("--watch", action="store_true",
                    help=_("enforce without the tray icon (apps from --apply or from the config); "
                           "Ctrl+C restores"))
    ap.add_argument("--dry-run", action="store_true", help=_("only show what would change"))
    ap.add_argument("--dump", metavar=_("FILE"),
                    help=_("write a diagnostic pw-dump without private data ('-' = stdout)"))
    ap.add_argument("--tray", action="store_true", help=_("tray icon (the default without arguments)"))
    ap.add_argument("-v", "--verbose", action="store_true", help=_("more log messages"))
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def streams_count(n: int) -> str:
    """"2 streams" / "2 strumienie" / "5 strumieni"."""
    return ngettext("{n} stream", "{n} streams", n).format(n=n)


# --------------------------------------------------------------------------
def _ids_to_serials(graph: Graph, ids: list[int] | None) -> tuple[list[int] | None, str | None]:
    if not ids:
        return None, None
    out = []
    for i in ids:
        n = graph.nodes.get(i)
        if n is None:
            return None, _("No node with id {id} (check --list).").format(id=i)
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
    sounds = [n for n in sorted(graph.nodes.values(), key=lambda n: n.serial) if is_discord_sound_stream(n, cfg)]
    group_ids = {c.id for c in det.group}
    feeding = {}
    for l in graph.links.values():
        if l.in_node in group_ids:
            feeding.setdefault(l.out_node, set()).add(l.in_node)

    if args.json:
        out = {
            "status": det.status,
            "status_text": status_label(det.status),
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
            "discord_sounds": [{"node_id": n.id, "feeds_capture": sorted(feeding.get(n.id, ()))}
                               for n in sounds],
            "config": {"selected_apps": config.selected_apps, "enforce": config.enforce,
                       "mute_discord_own_sounds": config.mute_discord_own_sounds,
                       "path": str(config.path)},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(status_label(det.status) + (f" ({det.message})" if det.message else ""))
    for w in det.warnings:
        print("  " + w)
    print()
    print(_("Discord capture streams:"))
    if not det.candidates:
        print("  " + _("(none)"))
    for c in det.candidates:
        mark = "*" if c.id in group_ids else " "
        print(f" {mark} {c.summary()}")
        print("      " + _("serial {serial}; reasons: {reasons}").format(serial=c.serial, reasons=c.reasons_text()))
        if c.needs_confirm and not c.confirmed_by(cfg):
            print("      " + _("only via an output monitor (it could be a microphone) — confirm with: --capture {id}")
                  .format(id=c.id))
        for l in graph.links_into(c.id):
            src = graph.nodes.get(l.out_node)
            sp = graph.ports.get(l.out_port)
            dp = graph.ports.get(l.in_port)
            tag = f" [{_('ours')}]" if l.managed else ""
            print(f"      <- {(src.name if src else '?')}#{l.out_node}:{sp.name if sp else '?'} -> {dp.name if dp else '?'}{tag}")
    print()
    print(_("Apps playing audio (name for --apply):"))
    if not apps:
        print("  " + _("(none)"))
    sel = {k.casefold() for k in config.selected_apps}
    for a in apps:
        box = "[x]" if a.key.casefold() in sel else "[ ]"
        ids = ",".join(f"#{n.id}" for n in a.nodes)
        extra = []
        if not a.has_pid:
            extra.append(_("no PID — Discord does not capture it by itself"))
        if any(n.id in feeding for n in a.nodes):
            extra.append(_("goes to Discord"))
        print(f"  {box} {a.display}  ({ids})" + (f"  — {'; '.join(extra)}" if extra else ""))
    print()
    print(discord_sounds_text(config.mute_discord_own_sounds) + "  (--discord-sounds on|off)")
    for n in sounds:
        print(f"  #{n.id} {n.name or '?'}" + (f"  — {_('goes to Discord')}" if n.id in feeding else ""))
    print()
    print(_("Config: {path}").format(path=config.path))
    print("  " + _("selected: {apps}; enforce at start: {enforce}").format(
        apps=", ".join(config.selected_apps) or _("(nothing)"),
        enforce=_("yes") if config.enforce else _("no")))
    return EXIT_OK


def _print_ops(plan) -> None:
    if not plan.ops:
        print(_("Nothing to change."))
    for op in plan.ops:
        verb = _("link") if op.kind == "link" else _("unlink")
        print(f"  {verb:<8} {op.desc}")
    for n in plan.notes:
        print("  " + _("(note: {note})").format(note=n))


def cmd_apply(pw, config: Config, args) -> int:
    graph = Graph.from_dump(pw.dump())
    cfg = config.detect_config()
    override, err = _ids_to_serials(graph, args.capture)
    if err:
        print(err, file=sys.stderr)
        return EXIT_USAGE
    keys, missing = _resolve_app_names(graph, cfg, args.apply)
    if missing and len(missing) == len(args.apply):
        # A typo must not mute the whole stream: without any known app nothing is changed.
        names = ", ".join(_("“{name}”").format(name=m) for m in missing)
        print(_("None of the given apps is playing right now ({names}) — not changing anything.")
              .format(names=names), file=sys.stderr)
        apps = list_apps(graph, cfg)
        if apps:
            print(_("Apps playing right now (name for --apply): {apps}")
                  .format(apps=", ".join(a.key for a in apps)), file=sys.stderr)
        else:
            print(_("No app is playing right now."), file=sys.stderr)
        return EXIT_USAGE
    for m in missing:
        print(_("Warning: app “{name}” is not playing right now — it will NOT go to Discord "
                "(check the name with --list).").format(name=m), file=sys.stderr)
    store = SnapshotStore()
    enf = Enforcer(pw, config, store, enabled=True, selected=keys, persist=False, dcfg=cfg)
    det = detect(graph, cfg, override, enf._known(graph))
    if not det.found:
        print(f"{status_label(det.status)}: {det.message}", file=sys.stderr)
        _print_candidates_hint(det)
        return EXIT_NO_CAPTURE
    plan = plan_apply(graph, det.group_nodes, keys, cfg, snapshot_originals(graph, store.data))
    print(_("Discord stream: {streams}").format(streams=", ".join(c.summary() for c in det.group)))
    _print_ops(plan)
    if args.dry_run:
        return EXIT_OK
    # --capture = the user's choice; confirming a "monitor-only" stream is remembered
    st = enf.set_capture_override(override) if override else enf.refresh("--apply")
    if st.error:
        print(_("Error: {error}").format(error=st.error), file=sys.stderr)
        return EXIT_ERROR
    print(_("Done: {action}. To undo: warpipe --restore").format(action=st.last_action or _("no changes")))
    return EXIT_OK


def _print_candidates_hint(det: Detection) -> None:
    if det.candidates:
        print(_("Candidates (pass --capture ID):"), file=sys.stderr)
        for c in det.candidates:
            print(f"  {c.summary()} — {c.reasons_text()}", file=sys.stderr)


def cmd_restore(pw, config: Config, args) -> int:
    store = SnapshotStore()
    if args.dry_run:
        graph = Graph.from_dump(pw.dump())
        _print_ops(plan_restore(graph, store.data, config.detect_config()))
        return EXIT_OK
    enf = Enforcer(pw, config, store, enabled=False, persist=False)
    ok, msg = enf.restore("--restore")
    if not ok:
        print(_("Restore error: {error}").format(error=msg), file=sys.stderr)
        return EXIT_ERROR
    print(_("Restored ({details}).").format(details=msg))
    return EXIT_OK


def cmd_watch(pw, config: Config, args) -> int:
    cfg = config.detect_config()
    if args.apply:
        # --watch --apply: exactly these apps; changes via the control channel are not saved
        keys, enabled, persist = list(args.apply), True, False
    else:
        # --watch without --apply: exactly like the tray icon — selection and mode from
        # the config, changes (e.g. from the OpenDeck plugin) are saved
        keys, enabled, persist = config.selected_apps, config.enforce, True
    if enabled and not keys:
        print(_("Warning: no app selected — nothing will go to Discord."), file=sys.stderr)
    enf = Enforcer(pw, config, SnapshotStore(), enabled=enabled, selected=keys, persist=persist, dcfg=cfg)
    if args.capture:
        graph = Graph.from_dump(pw.dump())
        override, err = _ids_to_serials(graph, args.capture)
        if err:
            print(err, file=sys.stderr)
            return EXIT_USAGE
        # only set it + remember the confirmation; the Watcher does the first refresh
        # (already with the SIGTERM/SIGINT handling that restores the state)
        enf.override = override
        enf._remember_confirmation(override)
    watcher = Watcher(enf, pw)

    def on_signal(signum, frame):
        log.info("signal %s — stopping and restoring", signum)
        watcher.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    if enabled:
        log.info("enforcing: %s", ", ".join(keys) or "(nothing)")
    else:
        log.info("\"Whole system\" mode (enforcement disabled in the config); waiting for commands")
    server = start_control_server(enf)
    try:
        watcher.run()
    finally:
        if server is not None:
            server.stop()
        ok, msg = enf.shutdown()
        if not ok:
            print(_("Restore error on exit: {error}").format(error=msg), file=sys.stderr)
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands for the running instance (control channel)
# --------------------------------------------------------------------------

def _print_status(status: dict) -> None:
    cap = status.get("capture") or {}
    label = _(CAPTURE_LABEL.get(cap.get("status"), STATUS_UNKNOWN_LABEL))
    n = cap.get("count") or 0
    print(label + (f" ({streams_count(n)})" if n > 1 else ""))
    apps = status.get("apps") or []
    chosen = [a["key"] for a in apps if a.get("selected")]
    print(routing_text(bool(status.get("enforce")), chosen))
    if "mute_discord_own_sounds" in status:
        print(discord_sounds_text(bool(status["mute_discord_own_sounds"])))
    if status.get("error"):
        print(_("Error: {error}").format(error=status["error"]))
    print()
    print(_("Apps:"))
    if not apps:
        print("  " + _("(no app is playing right now)"))
    for a in apps:
        box = "[x]" if a.get("selected") and status.get("enforce") else "[ ]"
        extra = []
        if not a.get("present"):
            extra.append(_("not playing now"))
        if a.get("in_stream"):
            extra.append(_("on stream"))
        print(f"  {box} {a.get('display') or a.get('key')}" + (f"  — {', '.join(extra)}" if extra else ""))


def _print_not_playing(reply: dict, app: str) -> None:
    """--toggle refused (like --apply with a typo): the error, the apps playing now, a hint."""
    print(_("Error: {error}").format(error=reply.get("error") or _("unknown error")), file=sys.stderr)
    status = reply.get("status") if isinstance(reply.get("status"), dict) else {}
    playing = [a.get("key") for a in status.get("apps") or []
               if isinstance(a, dict) and a.get("present") and isinstance(a.get("key"), str)]
    if playing:
        print(_("Apps playing right now: {apps}").format(apps=", ".join(playing)), file=sys.stderr)
    else:
        print(_("No app is playing right now."), file=sys.stderr)
    print(_("To select it anyway (e.g. before the game starts): warpipe --toggle {app} --force")
          .format(app=shlex.quote(app)), file=sys.stderr)


def cmd_remote(args) -> int:
    if args.status:
        req = {"cmd": "status"}
    elif args.system:
        req = {"cmd": "system"}
    else:
        # Without --force, turning on an app that is not playing is refused by the
        # running instance: in "Whole system" a typo would silence the whole stream.
        req = {"cmd": "toggle", "app": args.toggle, "require_present": not args.force}
    try:
        reply = control_request(req)
    except NotRunning as e:
        print(_("{error} Start it with: warpipe (tray icon) or warpipe --watch.").format(error=e), file=sys.stderr)
        return EXIT_NOT_RUNNING
    except ControlError as e:
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        return EXIT_ERROR
    if not reply.get("ok") and reply.get("code") == "not_playing":
        _print_not_playing(reply, args.toggle or "")
        return EXIT_USAGE
    if not reply.get("ok"):
        print(_("Error: {error}").format(error=reply.get("error") or _("unknown error")), file=sys.stderr)
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


def cmd_discord_sounds(pw, config: Config, args) -> int:
    """--discord-sounds on|off: in the running instance (applied at once and saved there),
    otherwise saved to the config; "on" then also brings back the links cut earlier."""
    allow = args.discord_sounds == "on"
    try:
        reply = control_request({"cmd": "discord_sounds", "on": allow})
    except NotRunning:
        reply = None
    except ControlError as e:
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        return EXIT_ERROR
    if reply is not None:
        if not reply.get("ok"):
            print(_("Error: {error}").format(error=reply.get("error") or _("unknown error")), file=sys.stderr)
            return EXIT_ERROR
        print(reply.get("message") or discord_sounds_text(not allow))
        return EXIT_OK
    lock = InstanceLock()
    if not lock.acquire():           # started meanwhile — try again later rather than race it
        print(_("Another instance is already running ({who}). Quit it or use its menu.")
              .format(who=_("tray icon or --watch")), file=sys.stderr)
        return EXIT_LOCKED
    try:
        enf = Enforcer(pw, config, SnapshotStore(), enabled=False)
        config.mute_discord_own_sounds = not allow
        try:
            config.save()
        except (OSError, ValueError) as e:
            print(_("Can't save settings: {error}").format(error=e), file=sys.stderr)
            return EXIT_ERROR
        if allow:
            enf.dcfg = config.detect_config()
            ok, msg = enf.restore_discord_sounds()
            if not ok:
                print(_("Restore error: {error}").format(error=msg), file=sys.stderr)
                return EXIT_ERROR
    finally:
        lock.release()
    print(discord_sounds_text(not allow))
    return EXIT_OK


def cmd_dump(pw, args) -> int:
    data = scrub_dump(pw.dump())
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.dump == "-":
        sys.stdout.write(text)
    else:
        with open(args.dump, "w", encoding="utf-8") as f:
            f.write(text)
        print(_("Saved {path} (without stream titles or personal data).").format(path=args.dump))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    modes = [m for m in ("list", "restore", "watch", "dump", "tray", "status", "system") if getattr(args, m)]
    if args.toggle is not None:
        modes.append("toggle")
    if args.discord_sounds is not None:
        modes.append("discord_sounds")
    if args.apply and not args.watch:
        modes.append("apply")
    if len(modes) > 1:
        ap.error(_("choose one of: --list, --apply, --restore, --watch, --dump, --tray, --status, "
                   "--toggle, --system, --discord-sounds (exception: --watch --apply)"))
    if args.json and not (args.list or args.status):
        ap.error(_("--json only works with --list or --status"))
    if args.force and args.toggle is None:
        ap.error(_("--force only works with --toggle"))
    if args.dry_run and not (args.apply or args.restore) or (args.dry_run and args.watch):
        ap.error(_("--dry-run works with --apply or --restore"))
    if modes and modes[0] in ("status", "toggle", "system"):
        if args.toggle is not None and not args.toggle.strip():
            ap.error(_("--toggle needs an app name (as shown by --list)"))
        if args.toggle is not None and not is_utf8_text(args.toggle):
            # e.g. a byte that is not UTF-8 in argv (Python keeps it as "\udcff")
            ap.error(_("--toggle: the app name is not valid UTF-8 text"))
        # Only talks to the running instance — no lock, no PipeWire, no config.
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
        print(_("Warning: {error} — using default settings.").format(error=config.load_error), file=sys.stderr)
    pw = PwCli()

    if mode == "tray":
        from .tray import run_tray  # PySide6 only when needed
        return run_tray(args, config, pw)
    if mode == "discord_sounds":
        return cmd_discord_sounds(pw, config, args)

    try:
        if mode == "list":
            return cmd_list(pw, config, args)
        if mode == "dump":
            return cmd_dump(pw, args)
        needs_lock = not args.dry_run
        lock = InstanceLock()
        if needs_lock and not lock.acquire():
            pid = lock.holder_pid()
            who = f"PID {pid}" if pid else _("tray icon or --watch")
            print(_("Another instance is already running ({who}). Quit it or use its menu.").format(who=who),
                  file=sys.stderr)
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
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_USAGE
