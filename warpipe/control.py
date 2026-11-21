"""Control channel of the running instance (tray icon or --watch).

Unix socket: $XDG_RUNTIME_DIR/warpipe/control.sock (directory 0700, socket 0600,
and the peer UID is checked too). Used by the OpenDeck plugin and by
`warpipe --status/--toggle/--system`.

The running instance's Enforcer stays the only owner of link changes. Through the
socket a client can only ask for a selection change (exactly the same methods as
the tray menu). Nothing from the socket is executed or interpreted as a system
command.

Protocol: UTF-8, one JSON message per line (max 4096 bytes).

  → {"cmd": "status"}
  → {"cmd": "toggle", "app": "Warframe.x64.exe"}
  → {"cmd": "set", "app": "Warframe.x64.exe", "on": true}
  → {"cmd": "system"}                      ("Whole system": disable enforcement and restore)
  → {"cmd": "subscribe"}                   (then the server pushes the state after every change)
  An optional "id" (number or short string) is echoed in the reply.
  toggle/set take an optional "require_present": true — turning on an app that is
  not playing right now is then refused (code "not_playing") instead of, in
  "Whole system" mode, cutting every app out of the stream.

  ← {"type": "reply", "id": …, "ok": true, "message": "…", "status": {…}}
  ← {"type": "reply", "id": …, "ok": false, "error": "…"[, "code": "…"][, "status": {…}]}
  ← {"type": "status", "status": {…}}      (only after subscribe)

  "message" and "error" are human-readable, in the instance's UI language.
  "code" (machine-readable, optional): "not_playing", "invalid_app".
  App names must be valid UTF-8 text without control characters (Unicode
  categories Cc, Cs, Zl, Zp); anything else is refused and the server keeps serving.

State:
  {"version": 1, "tool": "0.1.0", "enforce": bool,
   "capture": {"status": "active"|"none"|"ambiguous", "count": int},
   "apps": [{"key", "display", "present", "selected", "in_stream"}],
   "error": "…"}                            (error only when there is one)

  selected  — the app is in the saved selection (only effective with "enforce": true);
  in_stream — the app's output IS linked to the stream audio right now (graph state).

Threads: one accepts connections, each client has its own thread, and a separate
thread pushes the state to subscribers (with a short debounce). The Enforcer is
only used through its methods (RLock); in tray mode the state reaches the GUI via
a Qt signal, so none of these threads touches Qt objects.
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
import unicodedata
from pathlib import Path

from . import __version__
from .enforcer import AppNotPlaying, InvalidAppName
from .i18n import _
from .planner import STATUS_AMBIGUOUS, STATUS_FOUND, STATUS_INVALID
from .state import is_utf8_text, runtime_dir

log = logging.getLogger("warpipe")

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

CODE_NOT_PLAYING = "not_playing"
CODE_INVALID_APP = "invalid_app"

# Unicode categories refused in names: control characters (C0, DEL, C1), lone
# surrogates (they can't even be encoded as UTF-8), line and paragraph separators.
FORBIDDEN_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")


class ControlError(Exception):
    """Control channel error (the message is user-facing, translated)."""


class NotRunning(ControlError):
    """No instance with a control channel is running."""


class ProtocolError(ValueError):
    def __init__(self, msg: str, rid=None) -> None:
        super().__init__(msg)
        self.rid = rid


def socket_path() -> Path:
    return runtime_dir() / "control.sock"


# --------------------------------------------------------------------------
# State → JSON
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
    """State for channel clients. No media.name (titles) — only app keys."""
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
# Request parsing
# --------------------------------------------------------------------------

def _clean_text(value, what: str, limit: int, rid=None) -> str:
    if not isinstance(value, str):
        raise ProtocolError(_("field {field} must be a string").format(field=what), rid)
    v = value.strip()
    if not v:
        raise ProtocolError(_("field {field} is empty").format(field=what), rid)
    if len(v) > limit:
        raise ProtocolError(_("field {field} is too long (max {limit} characters)").format(field=what, limit=limit),
                            rid)
    if not is_utf8_text(v) or any(unicodedata.category(ch) in FORBIDDEN_CATEGORIES for ch in v):
        raise ProtocolError(_("field {field} contains control characters or invalid text").format(field=what), rid)
    return v


def _parse_on(value, rid) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("on", "off"):
        return value.strip().lower() == "on"
    raise ProtocolError(_("field on must be true/false or \"on\"/\"off\""), rid)


def _parse_flag(obj: dict, name: str, rid) -> bool:
    value = obj.get(name, False)
    if not isinstance(value, bool):
        raise ProtocolError(_("field {field} must be true or false").format(field=name), rid)
    return value


def parse_request(line) -> dict:
    """One line → {"cmd", "id", ...}. Errors: ProtocolError (with the id, if it could be read)."""
    if isinstance(line, str):
        line = line.encode("utf-8", "surrogatepass")
    if len(line) > MAX_LINE:
        raise ProtocolError(_("line too long (max {limit} bytes)").format(limit=MAX_LINE))
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError(_("invalid encoding (UTF-8 expected)")) from None
    try:
        obj = json.loads(text)
    except ValueError:
        raise ProtocolError(_("invalid JSON")) from None
    if not isinstance(obj, dict):
        raise ProtocolError(_("JSON object expected"))
    rid = obj.get("id")
    if not ((isinstance(rid, int) and not isinstance(rid, bool))
            or (isinstance(rid, str) and len(rid) <= MAX_ID_LEN)):
        rid = None
    cmd = obj.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise ProtocolError(_("missing field cmd"), rid)
    if cmd not in COMMANDS:
        shown = cmd if len(cmd) <= 40 else cmd[:40] + "…"
        raise ProtocolError(_("unknown command: {cmd} (known: {known})")
                            .format(cmd=repr(shown), known=", ".join(COMMANDS)), rid)
    req = {"cmd": cmd, "id": rid}
    if cmd in ("toggle", "set"):
        req["app"] = _clean_text(obj.get("app"), "app", MAX_APP_LEN, rid)
        req["require_present"] = _parse_flag(obj, "require_present", rid)
    if cmd == "set":
        req["on"] = _parse_on(obj.get("on"), rid)
    return req


# --------------------------------------------------------------------------
# Command handling (no sockets — directly testable)
# --------------------------------------------------------------------------

class ControlHandler:
    def __init__(self, enforcer) -> None:
        self.enforcer = enforcer

    def status(self) -> dict:
        return status_to_dict(self.enforcer.status)

    def _reply(self, st, message: str) -> dict:
        d = status_to_dict(st)
        out = {"type": "reply", "ok": not st.error, "status": d}
        if message:
            out["message"] = message
        if st.error:
            out["error"] = str(st.error)
        return out

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd in ("status", "subscribe"):
            return {"type": "reply", "ok": True, "status": self.status()}
        try:
            if cmd == "toggle":
                st, msg = self.enforcer.toggle_app(req["app"], require_present=bool(req.get("require_present")))
                return self._reply(st, msg)
            if cmd == "set":
                st, msg = self.enforcer.set_app(req["app"], bool(req["on"]),
                                                require_present=bool(req.get("require_present")))
                return self._reply(st, msg)
        except AppNotPlaying as e:
            return {"type": "reply", "ok": False, "code": CODE_NOT_PLAYING, "error": str(e),
                    "status": status_to_dict(e.status)}
        except InvalidAppName as e:
            return {"type": "reply", "ok": False, "code": CODE_INVALID_APP, "error": str(e)}
        if cmd == "system":
            st = self.enforcer.set_enabled(False)
            # on a failed restore st.error says so — no "all system audio" message then
            return self._reply(st, "" if st.error else _("All system audio goes to Discord."))
        return {"type": "reply", "ok": False, "error": _("unknown command: {cmd}").format(cmd=repr(cmd))}


def _dumps(obj) -> bytes:
    """One JSON line. Never raises on odd text: a lone surrogate (e.g. from a
    hand-edited config) becomes "?", so the output is always valid UTF-8 JSON."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "replace")


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

def _ensure_private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise ControlError(_("{path} is not a directory").format(path=d))
    if st.st_uid != os.getuid():
        raise ControlError(_("directory {path} belongs to another user").format(path=d))
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
    """Remove the socket left by an instance that died (e.g. kill -9). Returns True if removed.

    Call only AFTER acquiring the instance lock. If something is listening after all — error."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(st.st_mode):
        raise ControlError(_("{path} exists and is not a socket — leaving it alone").format(path=path))
    if st.st_uid != os.getuid():
        raise ControlError(_("socket {path} belongs to another user").format(path=path))
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError as e:
        if e.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise ControlError(_("can't check socket {path}: {error}").format(path=path, error=e)) from e
    else:
        raise ControlError(_("something is already listening on {path}").format(path=path))
    finally:
        probe.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    log.info("removed a stale control socket (the previous instance did not clean up)")
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
            raise ControlError(_("can't create socket {path}: {error}").format(path=self.path, error=e)) from e
        self._ident = (st.st_dev, st.st_ino)
        self._sock = s
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        for target, name in ((self._accept_loop, "accept"), (self._notify_loop, "notify")):
            t = threading.Thread(target=target, name=f"warpipe-control-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("control channel: %s", self.path)
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
        # Remove only OUR socket (same inode), not someone else's with the same name.
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
        """The state changed (called from any thread; does not block)."""
        self._dirty.set()

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # ------------------------------------------------------------- threads
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
            log.warning("control channel: rejecting a connection from UID %s", uid)
            conn.close()
            return
        _set_send_timeout(conn, SEND_TIMEOUT)
        client = _Client(conn)
        with self._clients_lock:
            too_many = len(self._clients) >= self.max_clients
            if not too_many:
                self._clients.add(client)
        if too_many:
            client.send({"type": "reply", "ok": False, "error": _("too many connections")})
            conn.close()
            return
        t = threading.Thread(target=self._client_loop, args=(client,),
                             name="warpipe-control-client", daemon=True)
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
                        try:
                            self._handle_line(client, line)
                        except Exception:
                            # one bad request must not end this client's connection
                            log.exception("control channel: error while handling a request")
                            client.send({"type": "reply", "ok": False, "error": _("internal error (see the log)")})
                if len(buf) > MAX_LINE:
                    client.send({"type": "reply", "ok": False,
                                 "error": _("line too long (max {limit} bytes) — disconnecting")
                                 .format(limit=MAX_LINE)})
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
            # Under the push lock: the reply (with the state) must not race with an
            # older state being pushed right now by the notify thread.
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
            log.exception("control channel: error during %s", req["cmd"])
            reply = {"type": "reply", "ok": False, "error": _("internal error (see the log)")}
        if req["id"] is not None:
            reply["id"] = req["id"]
        client.send(reply)
        if req["cmd"] != "status":
            log.info("control channel: %s %s → %s", req["cmd"], req.get("app", ""),
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
            try:
                self._push_all()
            except Exception:
                # the notify thread must survive anything, or subscribers go silent for good
                log.exception("control channel: pushing the state failed")

    def _push_all(self) -> None:
        with self._push_lock:
            with self._clients_lock:
                subs = [c for c in self._clients if c.subscribed]
            if not subs:
                return
            status = self.handler.status()
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
    """Start the control channel of the running instance. Call AFTER acquiring the lock.

    An error (e.g. a socket path that is too long) does not stop the tool — just a warning."""
    srv = ControlServer(ControlHandler(enforcer), path)
    try:
        srv.start()
    except (OSError, ControlError) as e:
        log.warning("control channel unavailable (the OpenDeck plugin and --toggle won't work): %s", e)
        return None
    enforcer.add_status_listener(srv.notify)
    return srv


# --------------------------------------------------------------------------
# Client (CLI)
# --------------------------------------------------------------------------

def request(req: dict, path: Path | None = None, timeout: float = 30.0) -> dict:
    """Send one command to the running instance and return the reply."""
    p = Path(path) if path is not None else socket_path()
    try:
        json.dumps(req, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as e:
        # e.g. an argv byte that is not UTF-8 ("\udcff"): refuse, don't send a "?"
        raise ControlError(_("The request contains text that is not valid UTF-8.")) from e
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(p))
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK, errno.ENOTDIR):
                raise NotRunning(_("Warpipe is not running (neither the tray icon nor --watch).")) from e
            raise ControlError(_("Can't connect to {path}: {error}").format(path=p, error=e)) from e
        s.sendall(_dumps(req))
        buf = b""
        while b"\n" not in buf:
            try:
                data = s.recv(65536)
            except socket.timeout as e:
                raise ControlError(_("The running instance did not answer in time.")) from e
            if not data:
                raise ControlError(_("The running instance closed the connection without answering."))
            buf += data
            if len(buf) > 4 * 1024 * 1024:
                raise ControlError(_("Reply too long."))
        line = buf.split(b"\n", 1)[0]
        try:
            obj = json.loads(line.decode("utf-8"))
        except ValueError as e:
            raise ControlError(_("Invalid reply from the running instance.")) from e
        if not isinstance(obj, dict):
            raise ControlError(_("Invalid reply from the running instance."))
        return obj
    finally:
        s.close()
