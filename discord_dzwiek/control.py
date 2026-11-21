"""Kanał sterowania działającą instancją (ikona albo --watch).

Gniazdo Unix: $XDG_RUNTIME_DIR/discord-dzwiek/control.sock (katalog 0700, gniazdo 0600,
dodatkowo sprawdzamy UID drugiej strony). Korzystają z niego wtyczka OpenDeck oraz
`discord-dzwiek --status/--toggle/--system`.

Jedynym właścicielem zmian połączeń pozostaje Enforcer działającej instancji. Przez
gniazdo można tylko poprosić o zmianę wyboru (dokładnie te same metody co menu
ikony). Nic z gniazda nie jest uruchamiane ani interpretowane jako polecenie systemowe.

Protokół: UTF-8, jedna wiadomość JSON w jednej linii (maks. 4096 bajtów).

  → {"cmd": "status"}
  → {"cmd": "toggle", "app": "Warframe.x64.exe"}
  → {"cmd": "set", "app": "Warframe.x64.exe", "on": true}
  → {"cmd": "system"}                      („Cały system”: wyłącz pilnowanie i przywróć)
  → {"cmd": "subscribe"}                   (potem serwer sam przysyła stan po każdej zmianie)
  Opcjonalne "id" (liczba albo krótki tekst) wraca w odpowiedzi.

  ← {"type": "reply", "id": …, "ok": true, "message": "…", "status": {…}}
  ← {"type": "reply", "id": …, "ok": false, "error": "…"}
  ← {"type": "status", "status": {…}}      (tylko po subscribe)

Stan:
  {"version": 1, "tool": "1.1.0", "enforce": bool,
   "capture": {"status": "active"|"none"|"ambiguous", "count": int},
   "apps": [{"key", "display", "present", "selected", "in_stream"}],
   "error": "…"}                            (error tylko gdy jest błąd)

  selected  — aplikacja jest w zapisanym wyborze (działa tylko przy "enforce": true);
  in_stream — wyjście aplikacji JEST teraz połączone z dźwiękiem streamu (stan grafu).

Wątki: jeden przyjmuje połączenia, każdy klient ma swój wątek, osobny wątek rozsyła
stan subskrybentom (z krótkim debounce). Z Enforcerem rozmawiamy tylko przez jego
metody (RLock); w trybie ikony stan do GUI idzie dalej sygnałem Qt, więc żaden
z tych wątków nie dotyka obiektów Qt.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import selectors
import socket
import stat
import struct
import threading
import time
from pathlib import Path

from . import __version__
from .planner import STATUS_AMBIGUOUS, STATUS_FOUND, STATUS_INVALID
from .state import runtime_dir

log = logging.getLogger("discord_dzwiek")

PROTOCOL_VERSION = 1
MAX_LINE = 4096
MAX_APP_LEN = 256
MAX_ID_LEN = 64
MAX_CLIENTS = 16
SEND_TIMEOUT = 2.0
DEBOUNCE = 0.15
MAX_DEBOUNCE = 1.0

CAPTURE_ACTIVE = "active"
CAPTURE_NONE = "none"
CAPTURE_AMBIGUOUS = "ambiguous"

COMMANDS = ("status", "toggle", "set", "system", "subscribe")


class ControlError(Exception):
    """Błąd kanału sterowania (komunikat po polsku)."""


class NotRunning(ControlError):
    """Nie działa żadna instancja z kanałem sterowania."""


class ProtocolError(ValueError):
    def __init__(self, msg: str, rid=None) -> None:
        super().__init__(msg)
        self.rid = rid


def socket_path() -> Path:
    return runtime_dir() / "control.sock"


# --------------------------------------------------------------------------
# Stan → JSON
# --------------------------------------------------------------------------

def capture_state(st) -> tuple[str, int]:
    det = st.detection
    if det is None:
        return CAPTURE_NONE, 0
    if det.status == STATUS_FOUND and det.group:
        return CAPTURE_ACTIVE, len(det.group)
    if det.status in (STATUS_AMBIGUOUS, STATUS_INVALID):
        return CAPTURE_AMBIGUOUS, 0
    return CAPTURE_NONE, 0


def status_to_dict(st) -> dict:
    """Stan dla klientów kanału. Bez media.name (tytułów) — tylko klucze aplikacji."""
    cap, count = capture_state(st)
    selected_cf = {k.casefold() for k in st.selected}
    in_cf = {k.casefold() for k in getattr(st, "in_stream", [])}
    apps = []
    seen = set()
    for a in st.apps:
        cf = a.key.casefold()
        seen.add(cf)
        apps.append({"key": a.key, "display": a.display, "present": True,
                     "selected": cf in selected_cf, "in_stream": cf in in_cf})
    for k in st.selected:
        if k.casefold() not in seen:
            seen.add(k.casefold())
            apps.append({"key": k, "display": k, "present": False,
                         "selected": True, "in_stream": False})
    out = {
        "version": PROTOCOL_VERSION,
        "tool": __version__,
        "enforce": bool(st.enabled),
        "capture": {"status": cap, "count": count},
        "apps": apps,
    }
    if st.error:
        out["error"] = str(st.error)
    return out


# --------------------------------------------------------------------------
# Parsowanie żądań
# --------------------------------------------------------------------------

def _clean_text(value, what: str, limit: int, rid=None) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"pole {what} musi być tekstem", rid)
    v = value.strip()
    if not v:
        raise ProtocolError(f"puste pole {what}", rid)
    if len(v) > limit:
        raise ProtocolError(f"pole {what} jest za długie (maks. {limit} znaków)", rid)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
        raise ProtocolError(f"pole {what} zawiera znaki sterujące", rid)
    return v


def _parse_on(value, rid) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("on", "off"):
        return value.strip().lower() == "on"
    raise ProtocolError("pole on musi być true/false albo \"on\"/\"off\"", rid)


def parse_request(line) -> dict:
    """Jedna linia → {"cmd", "id", ...}. Błędy: ProtocolError (z id, jeśli dało się je odczytać)."""
    if isinstance(line, str):
        line = line.encode("utf-8", "surrogatepass")
    if len(line) > MAX_LINE:
        raise ProtocolError(f"za długa linia (maks. {MAX_LINE} bajtów)")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("niepoprawne kodowanie (oczekiwane UTF-8)") from None
    try:
        obj = json.loads(text)
    except ValueError:
        raise ProtocolError("niepoprawny JSON") from None
    if not isinstance(obj, dict):
        raise ProtocolError("oczekiwany obiekt JSON")
    rid = obj.get("id")
    if not ((isinstance(rid, int) and not isinstance(rid, bool))
            or (isinstance(rid, str) and len(rid) <= MAX_ID_LEN)):
        rid = None
    cmd = obj.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise ProtocolError("brak pola cmd", rid)
    if cmd not in COMMANDS:
        shown = cmd if len(cmd) <= 40 else cmd[:40] + "…"
        raise ProtocolError(f"nieznane polecenie: {shown!r} (znane: {', '.join(COMMANDS)})", rid)
    req = {"cmd": cmd, "id": rid}
    if cmd in ("toggle", "set"):
        req["app"] = _clean_text(obj.get("app"), "app", MAX_APP_LEN, rid)
    if cmd == "set":
        req["on"] = _parse_on(obj.get("on"), rid)
    return req


# --------------------------------------------------------------------------
# Obsługa poleceń (bez gniazd — testowalne bezpośrednio)
# --------------------------------------------------------------------------

class ControlHandler:
    def __init__(self, enforcer) -> None:
        self.enforcer = enforcer

    def status(self) -> dict:
        return status_to_dict(self.enforcer.status)

    def _reply(self, st, message: str) -> dict:
        d = status_to_dict(st)
        out = {"type": "reply", "ok": not st.error, "message": message, "status": d}
        if st.error:
            out["error"] = str(st.error)
        return out

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd in ("status", "subscribe"):
            return {"type": "reply", "ok": True, "status": self.status()}
        if cmd == "toggle":
            st, msg = self.enforcer.toggle_app(req["app"])
            return self._reply(st, msg)
        if cmd == "set":
            st, msg = self.enforcer.set_app(req["app"], bool(req["on"]))
            return self._reply(st, msg)
        if cmd == "system":
            st = self.enforcer.set_enabled(False)
            return self._reply(st, "Do Discorda idzie cały dźwięk systemu.")
        return {"type": "reply", "ok": False, "error": f"nieznane polecenie: {cmd!r}"}


def _dumps(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# Serwer
# --------------------------------------------------------------------------

def _ensure_private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise ControlError(f"{d} nie jest katalogiem")
    if st.st_uid != os.getuid():
        raise ControlError(f"katalog {d} należy do innego użytkownika")
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)


def _peer_uid(conn: socket.socket) -> int | None:
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except (OSError, AttributeError, struct.error):
        return None


def _set_send_timeout(conn: socket.socket, seconds: float) -> None:
    try:
        sec = int(seconds)
        usec = int((seconds - sec) * 1_000_000)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", sec, usec))
    except (OSError, struct.error):
        pass


def remove_stale_socket(path: Path) -> bool:
    """Usuwa gniazdo po instancji, która padła (np. kill -9). Zwraca True, jeśli usunięto.

    Wołać dopiero PO zdobyciu blokady instancji. Jeśli ktoś jednak nasłuchuje — błąd."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(st.st_mode):
        raise ControlError(f"{path} istnieje i nie jest gniazdem — nie ruszam go")
    if st.st_uid != os.getuid():
        raise ControlError(f"gniazdo {path} należy do innego użytkownika")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError as e:
        if e.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise ControlError(f"nie mogę sprawdzić gniazda {path}: {e}") from e
    else:
        raise ControlError(f"na {path} już ktoś nasłuchuje")
    finally:
        probe.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    log.info("usunięto stare gniazdo sterowania (poprzednia instancja nie posprzątała)")
    return True


class _Client:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()
        self.subscribed = False
        self.last_status: bytes | None = None
        self.closed = False

    def send_locked(self, data: bytes) -> bool:
        if self.closed:
            return False
        try:
            self.sock.sendall(data)
            return True
        except OSError:
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return False

    def send(self, obj) -> bool:
        data = _dumps(obj)
        with self.lock:
            return self.send_locked(data)

    def shutdown(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class ControlServer:
    def __init__(self, handler: ControlHandler, path: Path | None = None, *,
                 debounce: float = DEBOUNCE, max_clients: int = MAX_CLIENTS) -> None:
        self.handler = handler
        self.path = Path(path) if path is not None else socket_path()
        self.debounce = debounce
        self.max_clients = max_clients
        self._sock: socket.socket | None = None
        self._ident: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._clients: set[_Client] = set()
        self._clients_lock = threading.Lock()
        self._push_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._client_threads: list[threading.Thread] = []
        self._wake_r = self._wake_w = -1
        self.pushes = 0

    # ------------------------------------------------------------- start/stop
    def start(self) -> "ControlServer":
        _ensure_private_dir(self.path.parent)
        remove_stale_socket(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(self.path))
            os.chmod(self.path, 0o600)
            s.listen(8)
            st = os.stat(self.path)
        except OSError as e:
            s.close()
            raise ControlError(f"nie mogę utworzyć gniazda {self.path}: {e}") from e
        self._ident = (st.st_dev, st.st_ino)
        self._sock = s
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        for target, name in ((self._accept_loop, "accept"), (self._notify_loop, "notify")):
            t = threading.Thread(target=target, name=f"discord-dzwiek-control-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("kanał sterowania: %s", self.path)
        return self

    def stop(self) -> None:
        if self._sock is None:
            return
        self._stop.set()
        self._dirty.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        for t in self._threads:
            t.join(timeout=3)
        with self._clients_lock:
            clients = list(self._clients)
        for c in clients:
            c.shutdown()
        for t in list(self._client_threads):
            t.join(timeout=3)
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
        # Usuwamy tylko NASZE gniazdo (to samo i-węzło), nie cudze o tej samej nazwie.
        try:
            st = os.lstat(self.path)
            if (st.st_dev, st.st_ino) == self._ident:
                os.unlink(self.path)
        except OSError:
            pass
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_r = self._wake_w = -1

    def notify(self, *_args) -> None:
        """Stan się zmienił (wołane z dowolnego wątku; nie blokuje)."""
        self._dirty.set()

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # ------------------------------------------------------------- wątki
    def _accept_loop(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(self._sock, selectors.EVENT_READ, "sock")
        sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        try:
            while not self._stop.is_set():
                for key, _ in sel.select():
                    if key.data == "wake":
                        try:
                            while os.read(self._wake_r, 64):
                                pass
                        except OSError:
                            pass
                        continue
                    try:
                        conn, _ = self._sock.accept()
                    except OSError:
                        continue
                    self._on_accept(conn)
        finally:
            sel.close()

    def _on_accept(self, conn: socket.socket) -> None:
        conn.setblocking(True)
        uid = _peer_uid(conn)
        if uid is not None and uid != os.getuid():
            log.warning("kanał sterowania: odrzucam połączenie od UID %s", uid)
            conn.close()
            return
        _set_send_timeout(conn, SEND_TIMEOUT)
        client = _Client(conn)
        with self._clients_lock:
            too_many = len(self._clients) >= self.max_clients
            if not too_many:
                self._clients.add(client)
        if too_many:
            client.send({"type": "reply", "ok": False, "error": "za dużo połączeń"})
            conn.close()
            return
        t = threading.Thread(target=self._client_loop, args=(client,),
                             name="discord-dzwiek-control-client", daemon=True)
        self._client_threads = [x for x in self._client_threads if x.is_alive()] + [t]
        t.start()

    def _client_loop(self, client: _Client) -> None:
        buf = b""
        try:
            while not self._stop.is_set() and not client.closed:
                try:
                    data = client.sock.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if line.strip():
                        self._handle_line(client, line)
                if len(buf) > MAX_LINE:
                    client.send({"type": "reply", "ok": False,
                                 "error": f"za długa linia (maks. {MAX_LINE} bajtów) — rozłączam"})
                    break
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.sock.close()
            except OSError:
                pass

    def _handle_line(self, client: _Client, line: bytes) -> None:
        try:
            req = parse_request(line)
        except ProtocolError as e:
            reply = {"type": "reply", "ok": False, "error": str(e)}
            if e.rid is not None:
                reply["id"] = e.rid
            client.send(reply)
            return
        if req["cmd"] == "subscribe":
            # Pod blokadą rozsyłania: odpowiedź (ze stanem) nie może się wyprzedzić
            # ze starszym stanem rozsyłanym właśnie przez wątek powiadomień.
            with self._push_lock:
                status = self.handler.status()
                reply = {"type": "reply", "ok": True, "status": status}
                if req["id"] is not None:
                    reply["id"] = req["id"]
                with client.lock:
                    client.subscribed = True
                    client.last_status = _dumps(status)
                    client.send_locked(_dumps(reply))
            return
        try:
            reply = self.handler.handle(req)
        except Exception:
            log.exception("kanał sterowania: błąd podczas %s", req["cmd"])
            reply = {"type": "reply", "ok": False, "error": "błąd wewnętrzny (szczegóły w logu)"}
        if req["id"] is not None:
            reply["id"] = req["id"]
        client.send(reply)
        if req["cmd"] != "status":
            log.info("kanał sterowania: %s %s → %s", req["cmd"], req.get("app", ""),
                     reply.get("message") or reply.get("error") or "")

    def _notify_loop(self) -> None:
        while True:
            self._dirty.wait()
            if self._stop.is_set():
                return
            start = time.monotonic()
            while True:
                self._dirty.clear()
                if self._stop.wait(self.debounce):
                    return
                if not self._dirty.is_set() or time.monotonic() - start >= MAX_DEBOUNCE:
                    break
            self._push_all()

    def _push_all(self) -> None:
        with self._push_lock:
            with self._clients_lock:
                subs = [c for c in self._clients if c.subscribed]
            if not subs:
                return
            try:
                status = self.handler.status()
            except Exception:  # pragma: no cover
                log.exception("kanał sterowania: stan")
                return
            data = _dumps(status)
            msg = _dumps({"type": "status", "status": status})
            for c in subs:
                with c.lock:
                    if c.last_status == data:
                        continue
                    if c.send_locked(msg):
                        c.last_status = data
                        self.pushes += 1


def start_control_server(enforcer, path: Path | None = None) -> ControlServer | None:
    """Uruchamia kanał sterowania dla działającej instancji. Wołać PO zdobyciu blokady.

    Błąd (np. za długa ścieżka gniazda) nie blokuje narzędzia — tylko ostrzeżenie."""
    srv = ControlServer(ControlHandler(enforcer), path)
    try:
        srv.start()
    except (OSError, ControlError) as e:
        log.warning("kanał sterowania niedostępny (wtyczka OpenDeck i --toggle nie zadziałają): %s", e)
        return None
    enforcer.add_status_listener(srv.notify)
    return srv


# --------------------------------------------------------------------------
# Klient (CLI)
# --------------------------------------------------------------------------

def request(req: dict, path: Path | None = None, timeout: float = 30.0) -> dict:
    """Wysyła jedno polecenie do działającej instancji i zwraca odpowiedź."""
    p = Path(path) if path is not None else socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(p))
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK, errno.ENOTDIR):
                raise NotRunning("Dźwięk do Discorda nie jest uruchomiony (ani ikona, ani --watch).") from e
            raise ControlError(f"Nie mogę połączyć się z {p}: {e}") from e
        s.sendall(_dumps(req))
        buf = b""
        while b"\n" not in buf:
            try:
                data = s.recv(65536)
            except socket.timeout as e:
                raise ControlError("Działająca instancja nie odpowiedziała na czas.") from e
            if not data:
                raise ControlError("Działająca instancja zamknęła połączenie bez odpowiedzi.")
            buf += data
            if len(buf) > 4 * 1024 * 1024:
                raise ControlError("Za długa odpowiedź.")
        line = buf.split(b"\n", 1)[0]
        try:
            obj = json.loads(line.decode("utf-8"))
        except ValueError as e:
            raise ControlError("Niepoprawna odpowiedź działającej instancji.") from e
        if not isinstance(obj, dict):
            raise ControlError("Niepoprawna odpowiedź działającej instancji.")
        return obj
    finally:
        s.close()
