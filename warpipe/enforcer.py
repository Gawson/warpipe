"""Link enforcement: state, plan execution, graph watching.

Graph watching: `pw-dump -m` is used as a "something changed" signal, and after
a short settle time (debounce) one full `pw-dump` gives a consistent picture of
the graph. Why not just poll every second: pw-dump every second means constant
wake-ups and ~0.4 MB of JSON to parse even when nothing happens, and the
reaction is still delayed. Why not build the state from -m deltas alone: deltas
are partial (e.g. params only) and removals are bare {"id": N, "info": null} —
easy to drift. The hybrid reacts in ~0.3 s, costs nothing when idle and always
plans on a full, consistent picture. On top of that there is a full refresh
every few seconds just in case, and polling every 2 s when the monitor dies.

Client events are ignored: each of our own pw-dump/pw-link calls creates a
client, so reacting to them would cause an endless loop.
"""

from __future__ import annotations

import codecs
import dataclasses
import json
import logging
import os
import selectors
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .graph import MANAGED_KEY, RESTORED_KEY, T_LINK, T_NODE, T_PORT, Graph, _as_int
from .i18n import _
from .planner import (AppEntry, Detection, DetectConfig, Op, app_key,
                      apps_in_stream, detect, link_entry, list_apps,
                      plan_apply, plan_restore, snapshot_originals,
                      status_label)
from .pw import PwError
from .state import Config, SnapshotStore, is_utf8_text

log = logging.getLogger("warpipe")

_UNSET = object()


class InvalidAppName(ValueError):
    """An app name that can't be stored or sent (empty, or not valid UTF-8 text)."""


class AppNotPlaying(Exception):
    """Refused: turning on an app that is not playing (require_present=True).

    In "Whole system" mode that would mean "only this app" — a typo would silence
    the whole stream. `status` is the state after a fresh look at the graph."""

    def __init__(self, key: str, status: "Status") -> None:
        super().__init__(_("App “{app}” is not playing right now — not changing anything.").format(app=key))
        self.key = key
        self.status = status


@dataclass
class Status:
    detection: Detection | None = None
    apps: list[AppEntry] = field(default_factory=list)
    enabled: bool = False
    selected: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    last_action: str = ""
    paused: bool = False
    retry_soon: bool = False  # e.g. an app has no ports yet — check again shortly
    # Keys of apps whose audio IS linked to the stream audio right now
    # (the actual graph state after a refresh, not just "selected").
    in_stream: list[str] = field(default_factory=list)

    @property
    def capture_status(self) -> str:
        if self.detection is None:
            return "error"
        return self.detection.status

    def headline(self) -> str:
        if self.error and self.detection is None:
            return _("PipeWire error")
        return status_label(self.capture_status)

    def routing_text(self) -> str:
        return routing_text(self.enabled, self.selected)

    def missing_selected(self) -> list[str]:
        present = {a.key.casefold() for a in self.apps}
        return [k for k in self.selected if k.casefold() not in present]


def routing_text(enabled: bool, selected: list[str]) -> str:
    """Where Discord's stream audio comes from, in one line."""
    if not enabled:
        return _("To Discord: all system audio")
    if not selected:
        return _("To Discord: nothing (stream audio muted)")
    return _("To Discord only: {apps}").format(apps=", ".join(selected))


EventCallback = Callable[[str, str], None]
StatusCallback = Callable[[Status], None]


class Enforcer:
    """Tool state + plan execution. Methods are thread-safe (RLock)."""

    def __init__(
        self,
        pw,
        config: Config,
        store: SnapshotStore,
        *,
        enabled: bool | None = None,
        selected: list[str] | None = None,
        persist: bool = True,
        dcfg: DetectConfig | None = None,
        on_status: StatusCallback | None = None,
        on_event: EventCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
        loop_limit: int = 15,
        loop_window: float = 30.0,
        loop_pause: float = 20.0,
    ) -> None:
        self.pw = pw
        self.config = config
        self.store = store
        self.persist = persist
        self.dcfg = dcfg or config.detect_config()
        self.enabled = config.enforce if enabled is None else bool(enabled)
        self.selected = list(config.selected_apps if selected is None else selected)
        self.override: list[int] | None = None
        self.on_status = on_status
        self.on_event = on_event
        self.clock = clock
        self.loop_limit = loop_limit
        self.loop_window = loop_window
        self.loop_pause = loop_pause
        self._lock = threading.RLock()
        self._last_group: set[int] = set()
        self._confirmed: set[int] = set()  # serials of nodes recognized as stream audio
        self._instance: object = _UNSET    # identity of the PipeWire session (Graph.instance_id)
        self._apply_times: deque[float] = deque()
        self._paused_until = 0.0
        self._last_error: str | None = None
        # Set when switching to "Whole system" could not restore the original links;
        # shown as the status error (while enforcement is off) until a restore succeeds.
        self._restore_error: str | None = None
        # Extra status listeners (e.g. the control channel). Called from the thread
        # that refreshed the state — they must be fast and must not block (ideally:
        # just set a flag and wake their own thread).
        self._status_listeners: list[StatusCallback] = []
        self.status = Status(enabled=self.enabled, selected=list(self.selected))
        if getattr(store, "load_error", None):
            log.warning("%s — starting with an empty snapshot", store.load_error)

    # ---------------------------------------------------------------- helpers
    def _emit(self, kind: str, msg: str) -> None:
        log.info("%s: %s", kind, msg)
        if self.on_event:
            try:
                self.on_event(kind, msg)
            except Exception:  # pragma: no cover
                log.exception("on_event")

    def _publish(self, status: Status) -> Status:
        self.status = status
        if self.on_status:
            try:
                self.on_status(status)
            except Exception:  # pragma: no cover
                log.exception("on_status")
        for cb in list(self._status_listeners):
            try:
                cb(status)
            except Exception:  # pragma: no cover
                log.exception("status listener")
        return status

    def add_status_listener(self, cb: StatusCallback) -> None:
        self._status_listeners.append(cb)

    def remove_status_listener(self, cb: StatusCallback) -> None:
        try:
            self._status_listeners.remove(cb)
        except ValueError:
            pass

    def _save_config(self) -> None:
        if not self.persist:
            return
        self.config.selected_apps = self.selected
        self.config.enforce = self.enabled
        try:
            self.config.save()
        except (OSError, ValueError) as e:  # ValueError: e.g. text that can't be encoded
            self._emit("error", _("Can't save settings: {error}").format(error=e))

    def _dump(self) -> Graph:
        return Graph.from_dump(self.pw.dump())

    def _sync_instance(self, graph: Graph) -> None:
        """After a PipeWire restart the numbers (object.serial) start over:
        forget everything that was remembered by serial."""
        inst = graph.instance_id
        if self._instance is not _UNSET and inst != self._instance:
            log.info("PipeWire restarted — forgetting old node numbers")
            self._confirmed.clear()
            self._last_group = set()
            self.override = None
        self._instance = inst
        if self.store.check_instance(inst):
            log.info("the link snapshot is from a different PipeWire session — discarding it")
            try:
                self.store.save()
            except OSError:
                pass

    def _prune_store(self, graph: Graph) -> None:
        live = {n.serial for n in graph.nodes.values() if n.media_class == "Stream/Input/Audio"}
        if self.store.prune(live):
            try:
                self.store.save()
            except OSError:
                pass

    def _verify_created(self, graph: Graph, ops: list[Op]) -> Graph:
        """Post-check: pw-link gets port numbers from an earlier pw-dump, and
        PipeWire reuses freed numbers right away. If a port disappeared in the
        meantime and another port got its number, our link went to the wrong
        place. Such a link (only one with OUR marker) is removed."""
        pairs = graph.link_pairs()
        bad = []
        for op in ops:
            l = pairs.get((op.out_port, op.in_port))
            if l is None or not l.ours:
                continue
            po, pi = graph.ports.get(op.out_port), graph.ports.get(op.in_port)
            if po is not None and pi is not None and po.serial == op.out_serial and pi.serial == op.in_serial:
                continue
            bad.append(op)
        if not bad:
            return graph
        for op in bad:
            log.warning("link went to a different port than planned (port number reused) "
                        "— removing: %s", op.desc)
            try:
                self.pw.unlink(op.out_port, op.in_port)
            except PwError as e:
                log.warning("failed to remove the wrong link: %s", e)
        return self._dump()

    # ------------------------------------------------------------- refresh
    def refresh(self, reason: str = "") -> Status:
        with self._lock:
            try:
                graph = self._dump()
            except PwError as e:
                msg = str(e)
                if msg != self._last_error:
                    self._emit("error", msg)
                self._last_error = msg
                return self._publish(Status(None, [], self.enabled, list(self.selected), error=msg))

            self._sync_instance(graph)
            known = self._known(graph)
            det = detect(graph, self.dcfg, self.override, known)
            if self.override and not any(s in graph.node_by_serial for s in self.override):
                log.info("the manually chosen node is gone — back to automatic detection")
                self.override = None
                det = detect(graph, self.dcfg, None, known)
            if det.found:
                self._confirmed |= {c.serial for c in det.group}

            error = None
            action = ""
            retry = False
            now = self.clock()
            paused = now < self._paused_until
            group_serials = {c.serial for c in det.group}
            appeared = bool(group_serials) and not self._last_group

            created: list[Op] = []
            if self.enabled and det.found and not paused:
                originals = snapshot_originals(graph, self.store.data)
                plan = plan_apply(graph, det.group_nodes, self.selected, self.dcfg, originals)
                for note in plan.notes:
                    log.debug("plan: %s", note)
                retry = bool(plan.notes)
                if plan.ops:
                    ok, action, created = self._execute_apply(graph, det, plan)
                    if not ok:
                        error = action
                    self._note_apply(now)
                    graph = None  # stale; do not use any further

            # Snapshots of nodes that are gone are useless (their links are gone too).
            try:
                g2 = graph if graph is not None else self._dump()
                if created:
                    g2 = self._verify_created(g2, created)
                self._prune_store(g2)
            except PwError:
                g2 = None

            apps = list_apps(g2, self.dcfg) if g2 is not None else []
            in_stream: list[str] = []
            if g2 is not None:
                self._confirmed &= set(g2.node_by_serial)
                det = detect(g2, self.dcfg, self.override, self._known(g2))
                in_stream = apps_in_stream(g2, det.group_nodes, apps)

            if appeared and self.enabled:
                self._emit("capture_found", _("Go Live audio detected.") + " " + self._routing_text())
            self._last_group = group_serials

            if error and error != self._last_error:
                self._emit("error", error)
            self._last_error = error
            warnings = list(det.warnings)
            if self.clock() < self._paused_until:
                warnings.append(_("Something keeps changing the links (Discord/WirePlumber?) — paused for a moment."))
            if error is None and not self.enabled and self._restore_error:
                # "Whole system" without the original links back must not look fine
                error = self._restore_error
            return self._publish(Status(
                detection=det, apps=apps, enabled=self.enabled, selected=list(self.selected),
                error=error, warnings=warnings, last_action=action,
                paused=self.clock() < self._paused_until, retry_soon=retry,
                in_stream=in_stream,
            ))

    def _known(self, graph: Graph) -> set[int]:
        known = set(self._confirmed)
        if self.store.instance == graph.instance_id:
            for k, cap in self.store.captures.items():
                n = graph.node_by_serial.get(_as_int(k))
                # serial + same node name + still a recording stream
                if n is not None and n.media_class == "Stream/Input/Audio" and cap.get("node_name") == n.name:
                    known.add(n.serial)
        return {s for s in known if s in graph.node_by_serial}

    def _routing_text(self) -> str:
        return Status(enabled=self.enabled, selected=list(self.selected)).routing_text()

    def _note_apply(self, now: float) -> None:
        self._apply_times.append(now)
        while self._apply_times and now - self._apply_times[0] > self.loop_window:
            self._apply_times.popleft()
        if len(self._apply_times) > self.loop_limit:
            self._paused_until = now + self.loop_pause
            self._apply_times.clear()
            self._emit("loop", _("Something keeps changing the links to Discord — pausing enforcement for a moment."))

    def _execute_apply(self, graph: Graph, det: Detection, plan) -> tuple[bool, str, list[Op]]:
        """Returns (ok, description, created ops — for the post-check)."""
        # 1. snapshot BEFORE any change
        self.store.check_instance(graph.instance_id)
        for c in det.group:
            entries = []
            for l in graph.links_into(c.id):
                if l.managed:
                    continue
                e = link_entry(graph, l)
                if e:
                    entries.append(e)
            self.store.ensure_capture(c.serial, c.node.name, entries)
        for op in plan.unlinks:
            if op.managed:
                continue
            l = graph.links.get(op.link_id)
            cap = graph.nodes.get(op.in_node)
            e = link_entry(graph, l, deleted=True) if l else None
            if e and cap:
                self.store.mark_deleted(cap.serial, e, cap.name)
        try:
            self.store.save()
        except OSError as e:
            return False, _("Can't save the link snapshot ({error}) — not changing anything.").format(error=e), []

        # 2. new links first (no gap in the audio), then removals
        created = deleted = 0
        created_ops: list[Op] = []
        restored_any = False
        result: tuple[bool, str] | None = None
        for op in plan.links:
            try:
                # An original from the snapshot is recreated WITHOUT the "managed" marker —
                # it is the pre-change state again and "Whole system" must keep it.
                r = self.pw.link(op.out_port, op.in_port, managed=op.managed)
            except PwError as e:
                log.warning("link failed: %s (%s)", op.desc, e)
                result = (False, _("{error} — not removing the remaining links.").format(error=e))
                break
            if r == "created":
                created += 1
                created_ops.append(op)
            if op.original:
                cap = graph.nodes.get(op.in_node)
                if cap is not None and self.store.mark_restored(cap.serial, op.out_serial, op.in_serial):
                    restored_any = True
            log.info("linked%s: %s", " (original)" if op.original else "", op.desc)
        if restored_any:
            try:
                self.store.save()
            except OSError:
                pass
        if result is not None:
            return result[0], result[1], created_ops
        errors = []
        for op in plan.unlinks:
            try:
                r = self.pw.unlink(op.out_port, op.in_port)
            except PwError as e:
                errors.append(str(e))
                continue
            deleted += r == "deleted"
            log.info("unlinked: %s", op.desc)
        action = _("linked {created}, unlinked {deleted}").format(created=created, deleted=deleted)
        if errors:
            return False, errors[0], created_ops
        return True, action, created_ops

    # ----------------------------------------------------------- fast path
    def fast_cut(self, objects: list[dict], new_link_ids) -> list[str]:
        """Cut NEW unwanted links into the stream audio at once (not after the debounce).

        Why: Discord plays each notification sound as a new short-lived stream, opens a
        discord_capture for it and WirePlumber links the two; waiting for the debounce
        and a full pw-dump lets viewers hear the "ding".

        `objects` is the graph as the pw-dump -m stream describes it right now (the
        MonitorParser view), `new_link_ids` the links that appeared in it. A link is
        cut only if ALL of these hold:
          - enforcement is on and not paused, and the view is the same PipeWire
            session as the last full refresh (ids are only compared within it);
          - the link ends in the current capture group (detect() on the view, so
            a brand-new discord_capture counts too; a microphone never does);
          - the full plan (plan_apply, the same as the refresh) removes it;
          - it is not ours (Warpipe's markers) and does not come from a selected app.
        The snapshot is updated first, exactly like a full apply, so "Whole system"
        brings such a link back (if its app is still playing).
        Returns the descriptions of the links removed."""
        new = {i for i in new_link_ids if isinstance(i, int)}
        with self._lock:
            if not new or not self.enabled or self.clock() < self._paused_until:
                return []
            graph = Graph.from_dump(objects)
            if self._instance is _UNSET or graph.instance_id != self._instance:
                return []
            det = detect(graph, self.dcfg, self.override, self._known(graph))
            if not det.found:
                return []
            selected = {k.casefold() for k in self.selected}
            plan = plan_apply(graph, det.group_nodes, self.selected, self.dcfg,
                              snapshot_originals(graph, self.store.data))
            cuts = []
            for op in plan.unlinks:
                link = graph.links.get(op.link_id)
                src = graph.nodes.get(op.out_node)
                if link is None or op.link_id not in new or link.ours:
                    continue
                if src is not None and app_key(src).casefold() in selected:
                    continue
                cuts.append(op)
            if not cuts:
                return []
            # 1. snapshot BEFORE the change (same as _execute_apply)
            for c in det.group:
                entries = [e for e in (link_entry(graph, l) for l in graph.links_into(c.id) if not l.managed) if e]
                self.store.ensure_capture(c.serial, c.node.name, entries)
            for op in cuts:
                e = link_entry(graph, graph.links[op.link_id], deleted=True)
                cap = graph.nodes.get(op.in_node)
                if e and cap is not None:
                    self.store.mark_deleted(cap.serial, e, cap.name)
            try:
                self.store.save()
            except OSError as e:
                log.warning("fast cut skipped — can't save the link snapshot: %s", e)
                return []
            # 2. remove
            done = []
            for op in cuts:
                try:
                    self.pw.unlink(op.out_port, op.in_port)
                except PwError as e:
                    log.warning("fast cut failed: %s (%s)", op.desc, e)
                    continue
                done.append(op.desc)
                log.info("cut a new link to the stream right away: %s", op.desc)
            if done:
                self._note_apply(self.clock())
            return done

    # ------------------------------------------------------------ restore
    def restore(self, reason: str = "") -> tuple[bool, str]:
        with self._lock:
            try:
                graph = self._dump()
            except PwError as e:
                return False, str(e)
            self._sync_instance(graph)   # snapshot from another PipeWire session → discarded
            self._prune_store(graph)     # snapshots of nodes that no longer exist
            plan = plan_restore(graph, self.store.data)
            for note in plan.notes:
                log.info("restore: %s", note)
            errors = []
            created = deleted = 0
            created_ops: list[Op] = []
            # Plan order: recreate, replace marked originals, remove ours.
            for op in plan.ops:
                try:
                    if op.kind == "link":
                        r = self.pw.link(op.out_port, op.in_port, managed=False)
                        if r == "created":
                            created += 1
                            created_ops.append(op)
                        log.info("restored: %s", op.desc)
                    else:
                        deleted += self.pw.unlink(op.out_port, op.in_port) == "deleted"
                        log.info("removed ours: %s", op.desc)
                except PwError as e:
                    errors.append(str(e))
            if created_ops:
                try:
                    self._verify_created(self._dump(), created_ops)
                except PwError:
                    pass
            if errors:
                return False, errors[0]
            self.store.clear()
            try:
                self.store.save()
            except OSError:
                pass
            self._restore_error = None
            return True, _("restored {created}, removed {deleted}").format(created=created, deleted=deleted)

    # ---------------------------------------------------------- UI/CLI actions
    def set_enabled(self, on: bool) -> Status:
        with self._lock:
            self.enabled = bool(on)
            self._paused_until = 0.0
            self._save_config()
            if on:
                st = self.refresh("enabled")
                if st.detection is not None and st.detection.found:
                    self._emit_applied()
                else:
                    self._emit_applied(_("Enabled. Takes effect when you start a Go Live stream with sound."))
                return st
            ok, msg = self.restore("disabled")
            if ok:
                self._emit("restored", _("All system audio goes to Discord."))
            else:
                self._restore_error = _("Restore failed: {error}").format(error=msg)
                self._emit("error", self._restore_error)
            # A failed restore stays in the status (error) until a restore succeeds,
            # so callers (control channel, CLI, plugin) don't report success.
            return self.refresh("after restore")

    def _emit_applied(self, prefix: str = "") -> None:
        """Tell the user where the stream audio comes from now. Enforcing with nothing
        selected keeps its meaning (nothing goes to Discord) but is never silent."""
        if self.enabled and not self.selected:
            self._emit("warning", _("No app selected — the stream has no audio until you tick one"))
            return
        self._emit("applied", (prefix + " " if prefix else "") + self._routing_text())

    @staticmethod
    def _check_key(key) -> None:
        """Refuse a name that could never be saved or sent — BEFORE any state changes."""
        if not isinstance(key, str) or not key.strip() or not is_utf8_text(key):
            raise InvalidAppName(_("Invalid app name (it must be non-empty UTF-8 text)."))

    def set_app_selected(self, key: str, on: bool) -> Status:
        with self._lock:
            self._check_key(key)
            sel = [k for k in self.selected if k.casefold() != key.casefold()]
            if on:
                sel.append(key)
            self.selected = sorted(sel, key=str.casefold)
            if on and not self.enabled:
                self.enabled = True
            self._paused_until = 0.0
            self._save_config()
            st = self.refresh("selection changed")
            if self.enabled:
                self._emit_applied()
            return st

    def set_selected(self, keys: list[str]) -> None:
        with self._lock:
            for k in keys:
                self._check_key(k)
            self.selected = list(keys)

    # ------------------------------------------------ toggling a single app
    # (OpenDeck plugin key, `--toggle`). Rules:
    #  - "Whole system" (enforcement off) + app X → enforcement on,
    #    selection = just {X};
    #  - enforcement on, X selected → unselect X; if nothing is left selected →
    #    back to "Whole system" (disable + restore);
    #  - enforcement on, X not selected → add X.
    # require_present=True (`--toggle` without --force, the plugin's key pressed while
    # Warpipe was not running): turning X on is refused with AppNotPlaying unless X
    # is playing right now — a typo must not cut every app out of the stream.

    def is_app_effective(self, key: str) -> bool:
        """Is X currently in effect (enforcement on and X selected)?"""
        with self._lock:
            return self.enabled and key.casefold() in {k.casefold() for k in self.selected}

    def _canonical_key(self, key: str) -> str:
        """The name as seen in the graph or in the saved selection (letter case)."""
        cf = key.casefold()
        for a in self.status.apps:
            if a.key.casefold() == cf:
                return a.key
        for k in self.selected:
            if k.casefold() == cf:
                return k
        return key

    def _is_present(self, key: str) -> bool:
        cf = key.casefold()
        return any(a.key.casefold() == cf for a in self.status.apps)

    def _require_present(self, key: str) -> str:
        """Returns the canonical key of a playing app; raises AppNotPlaying otherwise.

        The status may be stale (or not refreshed yet right after start-up), so an
        app that is not in it is looked up once more in a fresh pw-dump."""
        if not self._is_present(key):
            st = self.refresh("checking the app")
            key = self._canonical_key(key)
            if not self._is_present(key):
                raise AppNotPlaying(key, st)
        return key

    def toggle_app(self, key: str, require_present: bool = False) -> tuple[Status, str]:
        with self._lock:
            self._check_key(key)
            key = self._canonical_key(key)
            on = not self.is_app_effective(key)
            if on and require_present:
                key = self._require_present(key)
            return self._set_app_effective(key, on)

    def set_app(self, key: str, on: bool, require_present: bool = False) -> tuple[Status, str]:
        with self._lock:
            self._check_key(key)
            key = self._canonical_key(key)
            if self.is_app_effective(key) == bool(on):
                return self.status, _("No change: {routing}").format(routing=self._routing_text())
            if on and require_present:
                key = self._require_present(key)
            return self._set_app_effective(key, bool(on))

    def _set_app_effective(self, key: str, on: bool) -> tuple[Status, str]:
        if on:
            if not self.enabled:
                self.selected = [key]
                self.enabled = True
                self._paused_until = 0.0
                self._save_config()
                st = self.refresh("key: only this app")
                self._emit("applied", self._routing_text())
                return st, routing_text(True, [key])
            st = self.set_app_selected(key, True)
            return st, _("Added {app}.").format(app=key) + " " + self._routing_text()
        remaining = [k for k in self.selected if k.casefold() != key.casefold()]
        if not remaining:
            self.selected = []
            st = self.set_enabled(False)
            if st.error:
                # restore failed (st.error says why) — don't claim the whole system is back
                return st, _("Removed {app}.").format(app=key)
            return st, _("Removed {app}. Nothing is selected, so all system audio goes to Discord.").format(app=key)
        st = self.set_app_selected(key, False)
        return st, _("Removed {app}.").format(app=key) + " " + self._routing_text()

    def set_capture_override(self, serials: list[int] | None, remember: bool = True) -> Status:
        """Manual choice of the Discord stream (menu / --capture).

        remember=True: if the chosen stream was recognized only by an output
        monitor (it could be a microphone), the confirmation is remembered in the
        config (program, node.name, output sink) — next time it is picked automatically."""
        with self._lock:
            self.override = list(serials) if serials else None
            if serials and remember:
                self._remember_confirmation(list(serials))
            return self.refresh("stream chosen")

    def _remember_confirmation(self, serials: list[int]) -> None:
        try:
            graph = self._dump()
        except PwError:
            return
        det = detect(graph, self.dcfg, serials, self._known(graph))
        sigs = [s for c in det.group if c.needs_confirm for s in c.signatures()]
        if not sigs or not self.config.add_confirmed(sigs):
            return
        try:
            self.config.save()
        except (OSError, ValueError) as e:
            self._emit("error", _("Can't save settings: {error}").format(error=e))
        self.dcfg = dataclasses.replace(self.dcfg, confirmed=tuple(self.config.confirmed_captures))
        log.info("remembered the confirmed stream: %s",
                 "; ".join(f"{p} / {n} / {s}" for p, n, s in sigs))

    def shutdown(self) -> tuple[bool, str]:
        """On exit: restore the state from before our changes."""
        with self._lock:
            ok, msg = self.restore("shutdown")
            log.info("shutdown: %s", msg)
            return ok, msg


# --------------------------------------------------------------------------
# Graph watching
# --------------------------------------------------------------------------

class MonitorParser:
    """Parser for the `pw-dump -m -N` stream: consecutive JSON arrays ending with a "]" line.

    feed() returns True if the batch contained a relevant change (a node/port/link
    added or removed, or a change of a node's properties/ports).

    It also keeps `objects`: the current graph as the stream describes it (the
    first array is the full state, later ones add, update or remove objects), and
    remembers links that APPEARED after the first array — for the fast path
    (Enforcer.fast_cut). If a batch can't be parsed, the view is no longer trusted
    (`view_ok` False) until the monitor is restarted.
    """

    RELEVANT = {T_NODE, T_PORT, T_LINK}

    def __init__(self) -> None:
        self.buf = ""
        self._scan = 0
        self.known: dict[int, str] = {}
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.arrays = 0
        self.objects: dict[int, dict] = {}
        self.view_ok = True
        self._new_links: list[int] = []

    def feed(self, data: bytes) -> bool:
        self.buf += self._decoder.decode(data)
        relevant = False
        while True:
            start = max(0, self._scan - 2)
            p = self.buf.find("\n]", start)
            if p < 0 or p + 2 >= len(self.buf):
                self._scan = len(self.buf)
                break
            if self.buf[p + 2] not in "\r\n":
                self._scan = p + 2
                continue
            chunk = self.buf[: p + 2]
            self.buf = self.buf[p + 3:]
            self._scan = 0
            if not chunk.strip():
                continue
            try:
                arr = json.loads(chunk)
            except json.JSONDecodeError:
                log.debug("monitor: failed to parse a batch, refreshing just in case")
                relevant = True
                self.view_ok = False
                continue
            self.arrays += 1
            if self._classify(arr):
                relevant = True
        return relevant

    def _store(self, o: dict, oid, t, new: bool) -> None:
        """Update the view. Stored dicts are never modified later (a Graph built
        from them may still be in use) — an update makes a new merged dict."""
        prev = self.objects.get(oid)
        if new or prev is None:
            self.objects[oid] = o
            return
        merged = dict(prev)
        merged.update({k: v for k, v in o.items() if k != "info"})
        info = dict(prev.get("info") or {})
        info.update(o.get("info") or {})
        merged["info"] = info
        self.objects[oid] = merged

    def view(self) -> list[dict]:
        return list(self.objects.values())

    def take_new_capture_links(self) -> list[int]:
        """Links that appeared since the last call and end in a recording stream
        (Stream/Input/Audio) — not ours (Warpipe's markers). Cheap pre-filter for
        the fast path; the Enforcer decides on the full view."""
        ids, self._new_links = self._new_links, []
        if not self.view_ok:
            return []
        out = []
        for lid in ids:
            l = self.objects.get(lid)
            info = (l or {}).get("info") or {}
            props = info.get("props") or {}
            if (l is None or l.get("type") != T_LINK or MANAGED_KEY in props or RESTORED_KEY in props):
                continue
            dst = self.objects.get(info.get("input-node-id"))
            dprops = ((dst or {}).get("info") or {}).get("props") or {}
            if dst is not None and dst.get("type") == T_NODE and dprops.get("media.class") == "Stream/Input/Audio":
                out.append(lid)
        return out

    def _classify(self, arr) -> bool:
        if not isinstance(arr, list):
            return True
        relevant = False
        initial = self.arrays <= 1          # the first array is the state at start-up, not news
        for o in arr:
            if not isinstance(o, dict):
                continue
            oid = o.get("id")
            if "info" in o and o["info"] is None:
                t = self.known.pop(oid, None)
                self.objects.pop(oid, None)
                if t in self.RELEVANT:
                    relevant = True
                continue
            t = o.get("type")
            if t is None:
                continue
            prev = self.known.get(oid)
            self.known[oid] = t
            info = o.get("info") if isinstance(o.get("info"), dict) else {}
            cm = info.get("change-mask") or []
            # a new object: an id we didn't know as this type, or a first emission
            # (props in change-mask — also a removal + reuse of the id merged into one)
            new_obj = prev != t or (t in self.RELEVANT and "props" in cm)
            self._store(o, oid, t, new_obj)
            if t == T_LINK and new_obj and not initial:
                self._new_links.append(oid)
            if t not in self.RELEVANT:
                continue
            if prev != t:
                relevant = True
                continue
            # The first emission of an object has "props" in change-mask; later
            # param/state changes don't. Also protects against a removal and a
            # reuse of the same id being merged.
            if "props" in cm:
                relevant = True
            elif t == T_NODE and ("input-ports" in cm or "output-ports" in cm):
                relevant = True
        return relevant


class Watcher:
    """Loop: reads pw-dump -m and calls enforcer.refresh() after a debounce."""

    def __init__(
        self,
        enforcer,
        pw,
        *,
        debounce: float = 0.25,
        max_delay: float = 1.0,
        poll_interval: float = 15.0,
        fallback_poll: float = 2.0,
        retry_delay: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        fast_path: bool = True,
    ) -> None:
        self.enforcer = enforcer
        self.pw = pw
        self.fast_path = fast_path
        self.retry_delay = retry_delay
        self.max_retries = 6
        self.debounce = debounce
        self.max_delay = max_delay
        self.poll_interval = poll_interval
        self.fallback_poll = fallback_poll
        self.clock = clock
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._fd_lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self.refresh_count = 0

    def _wake(self) -> None:
        # Under the lock: after the pipe is closed its descriptor number could be
        # reused by another file — it must not be written to then.
        with self._fd_lock:
            if self._wake_w < 0:
                return
            try:
                os.write(self._wake_w, b"x")
            except (BlockingIOError, OSError):
                pass

    def request_refresh(self) -> None:
        self._kick.set()
        self._wake()

    def stop(self) -> None:
        self._stop.set()
        self._wake()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _fast_cut(self, parser: "MonitorParser") -> None:
        """New links into a recording stream: let the Enforcer cut unwanted ones NOW,
        without waiting for the debounce (the full refresh still follows)."""
        links = parser.take_new_capture_links()
        fast_cut = getattr(self.enforcer, "fast_cut", None)
        if not links or fast_cut is None or not getattr(self.enforcer, "enabled", False):
            return
        try:
            fast_cut(parser.view(), links)
        except Exception:
            log.exception("fast cut failed (the next full refresh fixes the links)")

    def _do_refresh(self) -> bool:
        """Returns True if another refresh shortly is worthwhile."""
        self.refresh_count += 1
        try:
            st = self.enforcer.refresh("graph changed")
        except Exception:
            log.exception("error while refreshing")
            return False
        return bool(getattr(st, "retry_soon", False))

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        proc = None
        parser = None
        started_at = 0.0
        failures = 0
        restart_at = 0.0
        dirty_since: float | None = None
        last_event = 0.0
        next_poll = self.clock()  # first refresh right away
        retries = 0
        try:
            while not self._stop.is_set():
                now = self.clock()
                if proc is None and now >= restart_at:
                    try:
                        proc = self.pw.start_monitor()
                        parser = MonitorParser()
                        sel.register(proc.stdout, selectors.EVENT_READ, "mon")
                        started_at = now
                    except PwError as e:
                        failures += 1
                        restart_at = now + min(60.0, 2.0 ** failures)
                        log.warning("graph monitor unavailable (%s) — polling every %.1f s", e, self.fallback_poll)
                        proc = None

                deadlines = [next_poll]
                if dirty_since is not None:
                    deadlines.append(min(last_event + self.debounce, dirty_since + self.max_delay))
                if proc is None:
                    deadlines.append(restart_at)
                timeout = max(0.0, min(deadlines) - self.clock())

                for key, _ in sel.select(timeout):
                    if key.data == "wake":
                        try:
                            while os.read(self._wake_r, 4096):
                                pass
                        except (BlockingIOError, OSError):
                            pass
                        if self._kick.is_set():
                            self._kick.clear()
                            t = self.clock()
                            dirty_since = dirty_since or t
                            last_event = t - self.debounce  # no waiting
                    elif key.data == "mon" and proc is not None:
                        try:
                            data = os.read(proc.stdout.fileno(), 65536)
                        except OSError:
                            data = b""
                        t = self.clock()
                        if not data:
                            sel.unregister(proc.stdout)
                            _terminate(proc)
                            proc = None
                            failures = failures + 1 if t - started_at < 5.0 else 1
                            restart_at = t + min(60.0, 2.0 ** failures)
                            log.warning("graph monitor exited — restarting in %.0f s", restart_at - t)
                            dirty_since = dirty_since or t
                            last_event = t
                        elif parser is not None:
                            relevant = parser.feed(data)
                            if self.fast_path:
                                self._fast_cut(parser)
                            if relevant:
                                dirty_since = dirty_since or t
                                last_event = t

                if self._stop.is_set():
                    break
                now = self.clock()
                due_dirty = dirty_since is not None and (
                    now - last_event >= self.debounce or now - dirty_since >= self.max_delay)
                if due_dirty or now >= next_poll:
                    dirty_since = None
                    retry = self._do_refresh()
                    interval = self.poll_interval if proc is not None else self.fallback_poll
                    retries = retries + 1 if retry else 0
                    if retry and retries <= self.max_retries:
                        interval = min(interval, self.retry_delay)
                    next_poll = self.clock() + interval
        finally:
            if proc is not None:
                try:
                    sel.unregister(proc.stdout)
                except (KeyError, ValueError):
                    pass
                _terminate(proc)
            sel.close()
            with self._fd_lock:
                for fd in (self._wake_r, self._wake_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._wake_r = self._wake_w = -1


def _terminate(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
