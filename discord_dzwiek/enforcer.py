"""Pilnowanie połączeń: stan, wykonywanie planu, obserwacja grafu.

Obserwacja grafu: `pw-dump -m` jako sygnał „coś się zmieniło”, a po krótkim
uspokojeniu (debounce) jeden pełny `pw-dump` jako spójny obraz grafu.
Dlaczego nie samo odpytywanie co 1 s: pw-dump co sekundę to ciągłe budzenie
procesu i ~0,4 MB JSON-a do parsowania nawet gdy nic się nie dzieje, a reakcja
i tak jest opóźniona. Dlaczego nie składanie stanu z samych delt -m: delty są
częściowe (np. same params), a usunięcia to gołe {"id": N, "info": null} —
łatwo o rozjazd. Hybryda daje reakcję w ~0,3 s, zerowy koszt w bezczynności
i zawsze pełny, spójny obraz do planowania. Dodatkowo co kilkanaście sekund
pełne odświeżenie na wszelki wypadek, a gdy monitor padnie — odpytywanie co 2 s.

Zdarzenia klientów (Client) są ignorowane: każde nasze wywołanie pw-dump/pw-link
tworzy klienta, więc reagowanie na nie dawałoby nieskończoną pętlę.
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

from .graph import T_LINK, T_NODE, T_PORT, Graph, _as_int
from .planner import (STATUS_FOUND, STATUS_LABEL_PL, AppEntry, Detection,
                      DetectConfig, Op, apps_in_stream, detect, link_entry,
                      list_apps, plan_apply, plan_restore, snapshot_originals)
from .pw import PwError
from .state import Config, SnapshotStore

log = logging.getLogger("discord_dzwiek")

_UNSET = object()


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
    retry_soon: bool = False  # np. aplikacja nie ma jeszcze portów — sprawdź za chwilę
    # Klucze aplikacji, których dźwięk JEST teraz połączony z dźwiękiem streamu
    # (stan faktyczny grafu po odświeżeniu, nie samo „zaznaczone”).
    in_stream: list[str] = field(default_factory=list)

    @property
    def capture_status(self) -> str:
        if self.detection is None:
            return "error"
        return self.detection.status

    def headline(self) -> str:
        if self.error and self.detection is None:
            return "Błąd PipeWire"
        return STATUS_LABEL_PL.get(self.capture_status, "Stream z dźwiękiem: ?")

    def routing_text(self) -> str:
        if not self.enabled:
            return "Do Discorda: cały dźwięk systemu"
        if not self.selected:
            return "Do Discorda: nic (nic nie zaznaczono)"
        return "Do Discorda tylko: " + ", ".join(self.selected)

    def missing_selected(self) -> list[str]:
        present = {a.key.casefold() for a in self.apps}
        return [k for k in self.selected if k.casefold() not in present]


EventCallback = Callable[[str, str], None]
StatusCallback = Callable[[Status], None]


class Enforcer:
    """Stan narzędzia + wykonanie planu. Metody bezpieczne wątkowo (RLock)."""

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
        self._confirmed: set[int] = set()  # serial węzłów rozpoznanych jako dźwięk streamu
        self._instance: object = _UNSET    # tożsamość uruchomienia PipeWire (Graph.instance_id)
        self._apply_times: deque[float] = deque()
        self._paused_until = 0.0
        self._last_error: str | None = None
        # Dodatkowi odbiorcy stanu (np. kanał sterowania). Wołani z wątku, który
        # odświeżył stan — muszą być szybcy i nie mogą blokować (najlepiej: tylko
        # ustawić flagę i obudzić własny wątek).
        self._status_listeners: list[StatusCallback] = []
        self.status = Status(enabled=self.enabled, selected=list(self.selected))
        if getattr(store, "load_error", None):
            log.warning("%s — zaczynam z pustą migawką", store.load_error)

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
        except OSError as e:
            self._emit("error", f"Nie mogę zapisać ustawień: {e}")

    def _dump(self) -> Graph:
        return Graph.from_dump(self.pw.dump())

    def _sync_instance(self, graph: Graph) -> None:
        """Po restarcie PipeWire numery (object.serial) zaczynają się od nowa:
        zapominamy wszystko, co było zapamiętane po serialach."""
        inst = graph.instance_id
        if self._instance is not _UNSET and inst != self._instance:
            log.info("PipeWire uruchomiony ponownie — zapominam stare numery węzłów")
            self._confirmed.clear()
            self._last_group = set()
            self.override = None
        self._instance = inst
        if self.store.check_instance(inst):
            log.info("migawka połączeń pochodzi z innego uruchomienia PipeWire — odrzucam ją")
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
        """Kontrola po wykonaniu: pw-link dostaje numery portów z wcześniejszego
        pw-dump, a PipeWire od razu używa zwolnionych numerów ponownie. Jeśli
        w międzyczasie port zniknął i jego numer dostał inny port, nasze połączenie
        trafiło nie tam. Takie połączenie (tylko z NASZYM znacznikiem) usuwamy."""
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
            log.warning("połączenie trafiło do innego portu niż planowano (numer portu użyty ponownie) "
                        "— usuwam: %s", op.desc)
            try:
                self.pw.unlink(op.out_port, op.in_port)
            except PwError as e:
                log.warning("nie udało się usunąć błędnego połączenia: %s", e)
        return self._dump()

    # ------------------------------------------------------------- odświeżanie
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
                log.info("ręcznie wybrany węzeł zniknął — wracam do wykrywania automatycznego")
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
                    graph = None  # nieaktualny; nie używać dalej

            # Migawki węzłów, które zniknęły, są bezużyteczne (połączenia zniknęły z nimi).
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
                self._emit("capture_found", "Wykryto stream z dźwiękiem. " + self._routing_text())
            self._last_group = group_serials

            if error and error != self._last_error:
                self._emit("error", error)
            self._last_error = error
            warnings = list(det.warnings)
            if self.clock() < self._paused_until:
                warnings.append("Coś ciągle zmienia połączenia (Discord/WirePlumber?) — chwilowo wstrzymane.")
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
                # serial + ta sama nazwa węzła + nadal strumień nagrywania
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
            self._emit("loop", "Coś ciągle zmienia połączenia do Discorda — wstrzymuję pilnowanie na chwilę.")

    def _execute_apply(self, graph: Graph, det: Detection, plan) -> tuple[bool, str, list[Op]]:
        """Zwraca (ok, opis, utworzone operacje — do kontroli po wykonaniu)."""
        # 1. migawka PRZED jakąkolwiek zmianą
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
            return False, f"Nie mogę zapisać migawki połączeń ({e}) — nic nie zmieniam.", []

        # 2. najpierw nowe połączenia (bez przerwy w dźwięku), potem usuwanie
        created = deleted = 0
        created_ops: list[Op] = []
        restored_any = False
        result: tuple[bool, str] | None = None
        for op in plan.links:
            try:
                # Oryginał z migawki odtwarzamy BEZ znacznika „managed” — to znowu stan
                # sprzed zmian i „Cały system” ma go zostawić.
                r = self.pw.link(op.out_port, op.in_port, managed=op.managed)
            except PwError as e:
                log.warning("link nieudany: %s (%s)", op.desc, e)
                result = (False, f"{e} — nie usuwam pozostałych połączeń.")
                break
            if r == "created":
                created += 1
                created_ops.append(op)
            if op.original:
                cap = graph.nodes.get(op.in_node)
                if cap is not None and self.store.mark_restored(cap.serial, op.out_serial, op.in_serial):
                    restored_any = True
            log.info("połączono%s: %s", " (oryginał)" if op.original else "", op.desc)
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
            log.info("rozłączono: %s", op.desc)
        action = f"połączono {created}, rozłączono {deleted}"
        if errors:
            return False, errors[0], created_ops
        return True, action, created_ops

    # ------------------------------------------------------------ przywracanie
    def restore(self, reason: str = "") -> tuple[bool, str]:
        with self._lock:
            try:
                graph = self._dump()
            except PwError as e:
                return False, str(e)
            self._sync_instance(graph)   # migawka z innego uruchomienia PipeWire → odrzucona
            self._prune_store(graph)     # migawki węzłów, których już nie ma
            plan = plan_restore(graph, self.store.data)
            for note in plan.notes:
                log.info("przywracanie: %s", note)
            errors = []
            created = deleted = 0
            created_ops: list[Op] = []
            # Kolejność z planu: odtworzenie, zamiana oznaczonych oryginałów, usunięcie naszych.
            for op in plan.ops:
                try:
                    if op.kind == "link":
                        r = self.pw.link(op.out_port, op.in_port, managed=False)
                        if r == "created":
                            created += 1
                            created_ops.append(op)
                        log.info("przywrócono: %s", op.desc)
                    else:
                        deleted += self.pw.unlink(op.out_port, op.in_port) == "deleted"
                        log.info("usunięto nasze: %s", op.desc)
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
            return True, f"przywrócono {created}, usunięto {deleted}"

    # ---------------------------------------------------------- akcje z UI/CLI
    def set_enabled(self, on: bool) -> Status:
        with self._lock:
            self.enabled = bool(on)
            self._paused_until = 0.0
            self._save_config()
            if on:
                st = self.refresh("włączono")
                if st.detection is not None and st.detection.found:
                    self._emit("applied", self._routing_text())
                else:
                    self._emit("applied", "Włączone. Zadziała, gdy zaczniesz stream z dźwiękiem. " + self._routing_text())
                return st
            ok, msg = self.restore("wyłączono")
            if ok:
                self._emit("restored", "Do Discorda idzie cały dźwięk systemu.")
            else:
                self._emit("error", f"Nie udało się przywrócić: {msg}")
            return self.refresh("po przywróceniu")

    def set_app_selected(self, key: str, on: bool) -> Status:
        with self._lock:
            sel = [k for k in self.selected if k.casefold() != key.casefold()]
            if on:
                sel.append(key)
            self.selected = sorted(sel, key=str.casefold)
            if on and not self.enabled:
                self.enabled = True
            self._paused_until = 0.0
            self._save_config()
            st = self.refresh("zmiana wyboru")
            if self.enabled:
                self._emit("applied", self._routing_text())
            return st

    def set_selected(self, keys: list[str]) -> None:
        with self._lock:
            self.selected = list(keys)

    # ------------------------------------------- przełączanie jednej aplikacji
    # (przycisk wtyczki OpenDeck, `--toggle`). Zasady:
    #  - „Cały system” (pilnowanie wyłączone) + aplikacja X → pilnowanie włączone,
    #    wybór = tylko {X};
    #  - pilnowanie włączone, X zaznaczona → odznacz X; jeśli nic nie zostało
    #    zaznaczone → z powrotem „Cały system” (wyłączenie + przywrócenie);
    #  - pilnowanie włączone, X niezaznaczona → dodaj X.

    def is_app_effective(self, key: str) -> bool:
        """Czy X jest teraz wybrana „na wyłączność” (pilnowanie włączone i X zaznaczona)."""
        with self._lock:
            return self.enabled and key.casefold() in {k.casefold() for k in self.selected}

    def _canonical_key(self, key: str) -> str:
        """Nazwa tak, jak ją widać w grafie albo w zapisanym wyborze (wielkość liter)."""
        cf = key.casefold()
        for a in self.status.apps:
            if a.key.casefold() == cf:
                return a.key
        for k in self.selected:
            if k.casefold() == cf:
                return k
        return key

    def toggle_app(self, key: str) -> tuple[Status, str]:
        with self._lock:
            key = self._canonical_key(key)
            return self._set_app_effective(key, not self.is_app_effective(key))

    def set_app(self, key: str, on: bool) -> tuple[Status, str]:
        with self._lock:
            key = self._canonical_key(key)
            if self.is_app_effective(key) == bool(on):
                return self.status, ("Bez zmian: " + self._routing_text())
            return self._set_app_effective(key, bool(on))

    def _set_app_effective(self, key: str, on: bool) -> tuple[Status, str]:
        if on:
            if not self.enabled:
                self.selected = [key]
                self.enabled = True
                self._paused_until = 0.0
                self._save_config()
                st = self.refresh("przycisk: tylko ta aplikacja")
                self._emit("applied", self._routing_text())
                return st, f"Do Discorda tylko: {key}"
            st = self.set_app_selected(key, True)
            return st, f"Dodano {key}. " + self._routing_text()
        remaining = [k for k in self.selected if k.casefold() != key.casefold()]
        if not remaining:
            self.selected = []
            st = self.set_enabled(False)
            return st, (f"Odznaczono {key}. Nic nie jest zaznaczone, więc do Discorda "
                        "idzie cały dźwięk systemu.")
        st = self.set_app_selected(key, False)
        return st, f"Odznaczono {key}. " + self._routing_text()

    def set_capture_override(self, serials: list[int] | None, remember: bool = True) -> Status:
        """Ręczny wybór strumienia Discorda (menu / --capture).

        remember=True: jeśli wybrany strumień był rozpoznany tylko po monitorze
        wyjścia (mógłby to być mikrofon), zapamiętujemy potwierdzenie w konfiguracji
        (program, node.name, wyjście) — następnym razem zostanie wybrany sam."""
        with self._lock:
            self.override = list(serials) if serials else None
            if serials and remember:
                self._remember_confirmation(list(serials))
            return self.refresh("wybór strumienia")

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
        except OSError as e:
            self._emit("error", f"Nie mogę zapisać ustawień: {e}")
        self.dcfg = dataclasses.replace(self.dcfg, confirmed=tuple(self.config.confirmed_captures))
        log.info("zapamiętano potwierdzony strumień: %s",
                 "; ".join(f"{p} / {n} / {s}" for p, n, s in sigs))

    def shutdown(self) -> tuple[bool, str]:
        """Przy wyjściu: przywróć stan sprzed naszych zmian."""
        with self._lock:
            ok, msg = self.restore("zamykanie")
            log.info("zamykanie: %s", msg)
            return ok, msg


# --------------------------------------------------------------------------
# Obserwacja grafu
# --------------------------------------------------------------------------

class MonitorParser:
    """Parser strumienia `pw-dump -m -N`: kolejne tablice JSON zakończone linią "]".

    feed() zwraca True, jeśli w paczce była istotna zmiana (węzeł/port/połączenie
    dodane lub usunięte, albo zmiana właściwości/portów węzła).
    """

    RELEVANT = {T_NODE, T_PORT, T_LINK}

    def __init__(self) -> None:
        self.buf = ""
        self._scan = 0
        self.known: dict[int, str] = {}
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.arrays = 0

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
                log.debug("monitor: nie udało się sparsować paczki, odświeżam na wszelki wypadek")
                relevant = True
                continue
            self.arrays += 1
            if self._classify(arr):
                relevant = True
        return relevant

    def _classify(self, arr) -> bool:
        if not isinstance(arr, list):
            return True
        relevant = False
        for o in arr:
            if not isinstance(o, dict):
                continue
            oid = o.get("id")
            if "info" in o and o["info"] is None:
                t = self.known.pop(oid, None)
                if t in self.RELEVANT:
                    relevant = True
                continue
            t = o.get("type")
            if t is None:
                continue
            prev = self.known.get(oid)
            self.known[oid] = t
            if t not in self.RELEVANT:
                continue
            if prev != t:
                relevant = True
                continue
            info = o.get("info") or {}
            cm = info.get("change-mask") or []
            # Pierwsza emisja obiektu ma "props" w change-mask; późniejsze zmiany
            # parametrów/stanu już nie. Chroni też przed sklejeniem usunięcia
            # i ponownego użycia tego samego id.
            if "props" in cm:
                relevant = True
            elif t == T_NODE and ("input-ports" in cm or "output-ports" in cm):
                relevant = True
        return relevant


class Watcher:
    """Pętla: czyta pw-dump -m, po debounce woła enforcer.refresh()."""

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
    ) -> None:
        self.enforcer = enforcer
        self.pw = pw
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
        # Pod blokadą: po zamknięciu potoku numer deskryptora mógłby zostać
        # użyty ponownie przez inny plik — nie wolno wtedy do niego pisać.
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

    def _do_refresh(self) -> bool:
        """Zwraca True, jeśli warto odświeżyć ponownie za chwilę."""
        self.refresh_count += 1
        try:
            st = self.enforcer.refresh("zmiana grafu")
        except Exception:
            log.exception("błąd podczas odświeżania")
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
        next_poll = self.clock()  # od razu pierwsze odświeżenie
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
                        log.warning("monitor grafu niedostępny (%s) — odpytuję co %.1f s", e, self.fallback_poll)
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
                            last_event = t - self.debounce  # bez czekania
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
                            log.warning("monitor grafu zakończył działanie — restart za %.0f s", restart_at - t)
                            dirty_since = dirty_since or t
                            last_event = t
                        elif parser is not None and parser.feed(data):
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
