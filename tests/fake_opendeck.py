#!/usr/bin/python3 -I
"""Atrapa OpenDeck do testów wtyczki (tylko biblioteka standardowa).

FakeOpenDeck:
  - serwer WebSocket na 127.0.0.1 (losowy port), minimalny RFC 6455 (tekst, ping, close);
  - uruchamia wtyczkę DOKŁADNIE jak OpenDeck 2.14 (plugins/mod.rs): plik z CodePathLin
    w katalogu wtyczki, cwd = katalog wtyczki, argumenty
    -port P -pluginUUID <katalog>.sdPlugin -registerEvent registerPlugin -info JSON;
  - wysyła zdarzenia w kształcie OpenDeck (willAppear, keyDown, sendToPlugin, ...);
  - zapisuje wszystko, co wtyczka przyśle (setImage, showAlert, sendToPropertyInspector...).

FakeControlServer: atrapa gniazda sterowania narzędzia (JSON w liniach), gdy testujemy
wtyczkę bez prawdziwego narzędzia i bez PipeWire.

Tryb podglądu (obrazki wszystkich stanów przycisków):
  python3 tests/fake_opendeck.py --preview opendeck-plugin/preview
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(ROOT, "opendeck-plugin", "io.github.gawson.warpipe.sdPlugin")
PLUGIN_UUID = os.path.basename(PLUGIN_DIR)          # OpenDeck: UUID wtyczki = nazwa katalogu
ACTION_APP = "io.github.gawson.warpipe.app"
ACTION_SYSTEM = "io.github.gawson.warpipe.system"
DEVICE = "sd-FAKE0001"
PROFILE = "Domyślny"
COLUMNS = 5
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TMP_BASE = os.environ.get("WARPIPE_TEST_TMP", os.environ.get("XDG_RUNTIME_DIR", "/tmp"))


# --------------------------------------------------------------------------
# PNG: metadane tEXt/iTXt/zTXt
# --------------------------------------------------------------------------

def png_text(data: bytes) -> dict:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("to nie PNG")
    out = {}
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"tEXt":
            k, _, v = body.partition(b"\0")
            out[k.decode("latin-1")] = v.decode("latin-1")
        elif ctype == b"zTXt":
            k, _, rest = body.partition(b"\0")
            out[k.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
        elif ctype == b"iTXt":
            k, _, rest = body.partition(b"\0")
            comp, _method = rest[0], rest[1]
            rest = rest[2:]
            _lang, _, rest = rest.partition(b"\0")
            _tkey, _, text = rest.partition(b"\0")
            if comp:
                text = zlib.decompress(text)
            out[k.decode("latin-1")] = text.decode("utf-8")
        elif ctype == b"IEND":
            break
    return out


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


def decode_data_url(url: str) -> bytes:
    prefix = "data:image/png;base64,"
    if not url.startswith(prefix):
        raise ValueError("oczekiwany data:image/png;base64,…")
    return base64.b64decode(url[len(prefix):], validate=True)


# --------------------------------------------------------------------------
# Minimalny WebSocket (serwer)
# --------------------------------------------------------------------------

class WsConn:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()
        self.buf = b""
        self.closed = False

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("koniec połączenia")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def handshake(self) -> None:
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("brak żądania HTTP")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        key = None
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        if not key:
            raise ConnectionError("brak Sec-WebSocket-Key")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
        with self.lock:
            if self.closed:
                raise ConnectionError("zamknięte")
            self.sock.sendall(head + payload)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str | None:
        """Następna wiadomość tekstowa; None = koniec połączenia."""
        parts = []
        while True:
            try:
                b0, b1 = self._read(2)
            except (ConnectionError, OSError):
                return None
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:
                try:
                    self._send_frame(0x8, b"")
                except (ConnectionError, OSError):
                    pass
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except (ConnectionError, OSError):
            pass
        with self.lock:
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# --------------------------------------------------------------------------
# Atrapa OpenDeck
# --------------------------------------------------------------------------

def context_for(pos: int) -> str:
    """Kontekst jak w OpenDeck: <urządzenie>.<profil>.<kontroler>.<pozycja>.<indeks>"""
    return f"{DEVICE}.{PROFILE}.Keypad.{pos}.0"


class FakeOpenDeck:
    def __init__(self) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.messages: list[dict] = []
        self.cond = threading.Condition()
        self.conn: WsConn | None = None
        self.proc: subprocess.Popen | None = None
        self.log_path: str | None = None
        self.registered_uuid: str | None = None
        self.connections = 0
        self._log_file = None
        threading.Thread(target=self._accept, daemon=True, name="fake-opendeck").start()

    # ------------------------------------------------------------ sieć
    def _accept(self) -> None:
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            ws = WsConn(s)
            try:
                ws.handshake()
                first = ws.recv_text()
            except (ConnectionError, OSError):
                s.close()
                continue
            try:
                reg = json.loads(first or "")
            except ValueError:
                reg = {}
            with self.cond:
                self.connections += 1
                if reg.get("event") == "registerPlugin":
                    self.conn = ws
                    self.registered_uuid = reg.get("uuid")
                self.messages.append(reg)
                self.cond.notify_all()
            threading.Thread(target=self._reader, args=(ws,), daemon=True).start()

    def _reader(self, ws: WsConn) -> None:
        while True:
            text = ws.recv_text()
            if text is None:
                with self.cond:
                    self.messages.append({"event": "_closed"})
                    self.cond.notify_all()
                return
            try:
                msg = json.loads(text)
            except ValueError:
                msg = {"event": "_invalid", "raw": text}
            with self.cond:
                self.messages.append(msg)
                self.cond.notify_all()

    def send(self, obj: dict) -> None:
        if self.conn is None:
            raise RuntimeError("wtyczka nie jest zarejestrowana")
        self.conn.send_text(json.dumps(obj, ensure_ascii=False))

    # ------------------------------------------------------------ wtyczka
    def launch(self, plugin_dir: str = PLUGIN_DIR, env: dict | None = None, log_path: str | None = None,
               uuid: str | None = None) -> subprocess.Popen:
        with open(os.path.join(plugin_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        code = (manifest.get("CodePaths") or {}).get("x86_64-unknown-linux-gnu") or manifest["CodePathLin"]
        exe = os.path.join(plugin_dir, code)
        info = {
            "application": {"font": "", "language": "pl", "platform": "linux",
                            "platformVersion": "fake", "version": "2.14.2"},
            "plugin": {"uuid": uuid or os.path.basename(plugin_dir), "version": manifest.get("Version")},
            "devicePixelRatio": 1,
            "colors": {},
            "devices": [{"id": DEVICE, "name": "Stream Deck", "size": {"rows": 3, "columns": COLUMNS}, "type": 0}],
        }
        args = [exe, "-port", str(self.port), "-pluginUUID", uuid or os.path.basename(plugin_dir),
                "-registerEvent", "registerPlugin", "-info", json.dumps(info)]
        self.log_path = log_path
        out = subprocess.DEVNULL
        if log_path:
            self._log_file = open(log_path, "ab")
            out = self._log_file
        self.proc = subprocess.Popen(args, cwd=plugin_dir, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT)
        return self.proc

    def wait_registered(self, timeout: float = 15.0) -> str:
        m = self.wait_for(lambda m: m.get("event") == "registerPlugin", timeout, what="registerPlugin")
        return m.get("uuid")

    def stop(self) -> int | None:
        """Zamyka połączenie (wtyczka powinna się sama zakończyć) i sprząta."""
        rc = None
        if self.conn is not None:
            self.conn.close()
        if self.proc is not None:
            try:
                rc = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    rc = self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    rc = self.proc.wait(timeout=3)
                rc = ("killed", rc)
        try:
            self.srv.close()
        except OSError:
            pass
        if self._log_file:
            self._log_file.close()
        return rc

    def plugin_log(self) -> str:
        if self.log_path and os.path.exists(self.log_path):
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        return ""

    # ------------------------------------------------------------ zdarzenia OpenDeck
    def _payload(self, pos: int, settings: dict | None) -> dict:
        return {"settings": settings or {}, "coordinates": {"row": pos // COLUMNS, "column": pos % COLUMNS},
                "controller": "Keypad", "state": 0, "isInMultiAction": False}

    def _ctx_event(self, event: str, ctx: str, action: str, settings: dict | None = None) -> None:
        pos = int(ctx.split(".")[3])
        self.send({"event": event, "action": action, "context": ctx, "device": DEVICE,
                   "payload": self._payload(pos, settings)})

    def will_appear(self, ctx, action, settings=None):
        self._ctx_event("willAppear", ctx, action, settings)

    def will_disappear(self, ctx, action, settings=None):
        self._ctx_event("willDisappear", ctx, action, settings)

    def key_down(self, ctx, action, settings=None):
        self._ctx_event("keyDown", ctx, action, settings)

    def key_up(self, ctx, action, settings=None):
        self._ctx_event("keyUp", ctx, action, settings)

    def did_receive_settings(self, ctx, action, settings):
        self._ctx_event("didReceiveSettings", ctx, action, settings)

    def did_receive_global_settings(self, settings):
        self.send({"event": "didReceiveGlobalSettings", "payload": {"settings": settings}})

    def pi_did_appear(self, ctx, action):
        self.send({"event": "propertyInspectorDidAppear", "action": action, "context": ctx, "device": DEVICE})

    def pi_did_disappear(self, ctx, action):
        self.send({"event": "propertyInspectorDidDisappear", "action": action, "context": ctx, "device": DEVICE})

    def send_to_plugin(self, ctx, action, payload):
        """Jak OpenDeck przekazujący sendToPlugin z inspektora właściwości."""
        self.send({"event": "sendToPlugin", "action": action, "context": ctx, "payload": payload})

    # ------------------------------------------------------------ oczekiwanie / odczyt
    def mark(self) -> int:
        with self.cond:
            return len(self.messages)

    def wait_for(self, pred, timeout: float = 5.0, since: int = 0, what: str = "") -> dict:
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                for m in self.messages[since:]:
                    if pred(m):
                        return m
                left = end - time.monotonic()
                if left <= 0:
                    tail = [m.get("event") for m in self.messages[-12:]]
                    raise TimeoutError(f"nie doczekałem się: {what or pred} (ostatnie: {tail})\n"
                                       f"log wtyczki:\n{self.plugin_log()[-3000:]}")
                self.cond.wait(left)

    def found(self, pred, since: int = 0) -> list[dict]:
        with self.cond:
            return [m for m in self.messages[since:] if pred(m)]

    def images(self, ctx: str, since: int = 0) -> list[dict]:
        out = []
        for m in self.found(lambda m: m.get("event") == "setImage" and m.get("context") == ctx, since):
            png = decode_data_url(m["payload"]["image"])
            meta = png_text(png)
            out.append({"msg": m, "png": png, "meta": meta, "state": meta.get("ddstate"),
                        "ring": meta.get("ddring") == "1", "label": meta.get("ddlabel"),
                        "badge": meta.get("ddbadge")})
        return out

    def last_image(self, ctx: str) -> dict | None:
        imgs = self.images(ctx)
        return imgs[-1] if imgs else None

    def wait_image(self, ctx: str, state: str, timeout: float = 5.0, ring: bool | None = None) -> dict:
        """Czeka, aż OSTATNI obrazek przycisku ma dany stan (np. "app:on")."""
        end = time.monotonic() + timeout
        while True:
            img = self.last_image(ctx)
            if img and img["state"] == state and (ring is None or img["ring"] == ring):
                return img
            left = end - time.monotonic()
            if left <= 0:
                got = (img["state"], img["ring"]) if img else None
                raise TimeoutError(f"{ctx}: oczekiwany {state} (ring={ring}), jest {got}\n"
                                   f"log wtyczki:\n{self.plugin_log()[-3000:]}")
            with self.cond:
                self.cond.wait(min(left, 0.2))


# --------------------------------------------------------------------------
# Atrapa gniazda sterowania narzędzia
# --------------------------------------------------------------------------

class FakeControlServer:
    """Odpowiada jak narzędzie: subscribe → stan; toggle/system → zapisuje polecenie."""

    def __init__(self, path: str, status: dict) -> None:
        self.path = path
        self.status = status
        self.commands: list[dict] = []
        self.reply_ok = True
        self.lock = threading.Lock()
        self.clients: list[socket.socket] = []
        self.subs: list[socket.socket] = []
        self.srv: socket.socket | None = None

    def start(self) -> "FakeControlServer":
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(4)
        threading.Thread(target=self._accept, daemon=True, name="fake-control").start()
        return self

    def _accept(self) -> None:
        srv = self.srv
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(c)
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _send(self, c: socket.socket, obj: dict) -> None:
        try:
            c.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass

    def _client(self, c: socket.socket) -> None:
        f = c.makefile("rb")
        while True:
            try:
                line = f.readline()
            except OSError:
                return
            if not line:
                return
            try:
                req = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                self.commands.append(req)
                st = self.status
                ok = self.reply_ok
            reply = {"type": "reply", "id": req.get("id"), "ok": ok, "status": st}
            if not ok:
                reply["error"] = "atrapa: błąd"
            if req.get("cmd") == "subscribe":
                reply["ok"] = True
                with self.lock:
                    self.subs.append(c)
            self._send(c, reply)

    def set_status(self, st: dict) -> None:
        with self.lock:
            self.status = st
            subs = list(self.subs)
        for c in subs:
            self._send(c, {"type": "status", "status": st})

    def wait_command(self, pred, timeout: float = 5.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for c in self.commands:
                    if pred(c):
                        return c
            time.sleep(0.02)
        raise TimeoutError(f"brak polecenia; są: {self.commands}")

    def stop(self) -> None:
        if self.srv is not None:
            try:
                self.srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.srv.close()
            self.srv = None
        with self.lock:
            clients, self.clients, self.subs = self.clients, [], []
        for c in clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def make_status(enforce=False, capture="active", apps=(), error=None) -> dict:
    """apps: krotki (key, present, selected, in_stream)."""
    st = {"version": 1, "tool": "test", "enforce": enforce,
          "capture": {"status": capture, "count": 1 if capture == "active" else 0},
          "apps": [{"key": k, "display": k, "present": p, "selected": s, "in_stream": i} for k, p, s, i in apps]}
    if error:
        st["error"] = error
    return st


def plugin_env(runtime_dir: str, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM",)}
    env["DISCORD_DZWIEK_RUNTIME_DIR"] = runtime_dir
    env["QT_QPA_PLATFORM"] = "offscreen"
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------
# Podgląd: obrazki wszystkich stanów
# --------------------------------------------------------------------------

def preview(outdir: str) -> int:
    os.makedirs(TMP_BASE, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ddp-preview.", dir=TMP_BASE)
    run = os.path.join(tmp, "run")
    sock = os.path.join(run, "control.sock")
    launcher = os.path.join(tmp, "fake-launcher")
    with open(launcher, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(launcher, 0o755)
    os.makedirs(outdir, exist_ok=True)
    od = FakeOpenDeck()
    ctl = None
    saved = []
    try:
        od.launch(env=plugin_env(run), log_path=os.path.join(tmp, "plugin.log"))
        od.wait_registered()
        game, sysk, unset = context_for(0), context_for(1), context_for(2)
        od.will_appear(game, ACTION_APP, {"app": "Warframe.x64.exe"})
        od.will_appear(sysk, ACTION_SYSTEM, {})
        od.will_appear(unset, ACTION_APP, {})

        def save(ctx, state, name, ring=None):
            img = od.wait_image(ctx, state, timeout=10, ring=ring)
            path = os.path.join(outdir, name + ".png")
            with open(path, "wb") as f:
                f.write(img["png"])
            saved.append((name, img))
            print(f"  {name}.png  ({img['state']}, ramka={int(img['ring'])}, „{img['badge']}”)")

        print(f"zapisuję podgląd do {outdir}")
        save(game, "app:down", "app-01-narzedzie-nie-dziala")
        save(sysk, "system:down", "system-01-narzedzie-nie-dziala")
        save(unset, "app:unset", "app-00-bez-aplikacji")
        od.did_receive_global_settings({"toolPath": launcher})
        time.sleep(0.3)
        od.key_down(game, ACTION_APP, {"app": "Warframe.x64.exe"})
        save(game, "app:starting", "app-02-uruchamiam")

        ctl = FakeControlServer(sock, make_status(False, "none", [("Warframe.x64.exe", True, False, False)])).start()
        save(game, "app:nostream", "app-03-brak-streamu", ring=False)
        ctl.set_status(make_status(True, "none", [("Warframe.x64.exe", True, True, False)]))
        save(game, "app:nostream", "app-04-brak-streamu-wybrana", ring=True)
        ctl.set_status(make_status(False, "active", [("Warframe.x64.exe", True, False, True)]))
        save(game, "app:on", "app-05-na-streamie")
        save(sysk, "system:sys_on", "system-02-caly-system-wlaczony")
        ctl.set_status(make_status(True, "active", [("Warframe.x64.exe", True, False, False),
                                                    ("Firefox", True, True, True)]))
        save(game, "app:off", "app-06-gra-nie-na-streamie", ring=False)
        save(sysk, "system:sys_off", "system-03-tylko-wybrane")
        ctl.set_status(make_status(True, "active", [("Firefox", True, True, True)]))
        save(game, "app:absent", "app-07-nie-gra", ring=False)
        ctl.set_status(make_status(True, "active", [("Warframe.x64.exe", False, True, False)]))
        save(game, "app:absent", "app-08-nie-gra-wybrana", ring=True)
        ctl.set_status(make_status(False, "ambiguous", [("Warframe.x64.exe", True, False, False)]))
        save(game, "app:unsure", "app-09-stream-niepewny")
        ctl.set_status(make_status(False, "none", [], error="Nie mogę odczytać stanu PipeWire"))
        save(game, "app:error", "app-10-blad")
        _contact_sheet(outdir, saved)
    finally:
        if ctl is not None:
            ctl.stop()
        rc = od.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"wtyczka zakończyła się: {rc}")
    return 0


def _contact_sheet(outdir: str, saved) -> None:
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
    except ImportError:  # pragma: no cover
        return
    app = QGuiApplication.instance() or QGuiApplication(["preview", "-platform", "offscreen"])  # noqa: F841
    cols, cell, cap = 5, 160, 34
    rows = (len(saved) + cols - 1) // cols
    sheet = QImage(cols * cell + 8, rows * (cell + cap) + 8, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#101010"))
    p = QPainter(sheet)
    f = QFont()
    f.setPixelSize(11)
    p.setFont(f)
    for i, (name, img) in enumerate(saved):
        x, y = 8 + (i % cols) * cell, 8 + (i // cols) * (cell + cap)
        im = QImage.fromData(img["png"], "PNG")
        p.drawImage(x, y, im)
        p.setPen(QColor("#cccccc"))
        p.drawText(QRectF(x, y + 146, cell - 8, cap), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                   name)
    p.end()
    sheet.save(os.path.join(outdir, "wszystkie.png"))
    print("  wszystkie.png  (zestawienie)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Atrapa OpenDeck dla wtyczki Warpipe")
    ap.add_argument("--preview", metavar="KATALOG", help="zapisz obrazki wszystkich stanów przycisków")
    args = ap.parse_args(argv)
    if args.preview:
        return preview(args.preview)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
