"""Plugin process: WebSocket to OpenDeck (Stream Deck SDK v2) + the tool's control socket.

OpenDeck starts the plugin as a plain program (in the plugin directory):
  warpipe-plugin -port P -pluginUUID U -registerEvent registerPlugin -info JSON
The plugin connects to ws://127.0.0.1:P, sends {"event": "registerPlugin", "uuid": U}
and receives events (willAppear, keyDown, sendToPlugin, ...).

It talks to the tool (tray icon or --watch) over
$XDG_RUNTIME_DIR/warpipe/control.sock: subscribes to the state and only sends
"toggle <app>" or "system". The plugin never changes PipeWire links itself.

When OpenDeck closes the connection, the plugin exits.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QLibraryInfo, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWebSockets import QWebSocket

from . import __version__
from .logic import (ACTION_APP, ACTION_SYSTEM, apps_for_pi, child_env,
                    control_socket_path, normalize_status, press_for,
                    resolve_language, resolve_tool, sanitize_global_settings,
                    sanitize_settings, set_language, tr, visual_for)
from .render import Renderer

log = logging.getLogger("warpipe_plugin")

ACTIONS = (ACTION_APP, ACTION_SYSTEM)
MAX_CONTEXT_LEN = 512
MAX_CTL_BUFFER = 4 * 1024 * 1024
RETRY_MS = 2000
RETRY_STARTING_MS = 500
STARTING_S = 20.0
LAUNCH_COOLDOWN_S = 5.0
FIRST_STATUS_WAIT_MS = 1500


class ControlClient(QObject):
    """Connection to the tool. status(None) = the tool is not running."""

    status = Signal(object)
    reply = Signal(object)

    def __init__(self, path: str, retry_ms: int = RETRY_MS) -> None:
        super().__init__()
        self.path = path
        self.retry_ms = retry_ms
        self.up: bool | None = None       # None = not known yet
        self._buf = b""
        self.sock = QLocalSocket(self)
        self.sock.connected.connect(self._on_connected)
        self.sock.disconnected.connect(self._on_disconnected)
        self.sock.readyRead.connect(self._on_ready)
        self.sock.errorOccurred.connect(self._on_error)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._try_connect)

    def start(self) -> None:
        self._try_connect()

    def retry_soon(self, ms: int = 300) -> None:
        self._timer.start(ms)

    def _try_connect(self) -> None:
        if self.sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self._buf = b""
        self.sock.connectToServer(self.path)

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start(self.retry_ms)

    def _mark_down(self) -> None:
        if self.up is not False:
            self.up = False
            self.status.emit(None)
        self._schedule()

    def send(self, obj: dict) -> bool:
        if self.sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return False
        self.sock.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.sock.flush()
        return True

    def _on_connected(self) -> None:
        log.info("connected to the tool (%s)", self.path)
        self.send({"cmd": "subscribe", "id": "sub"})

    def _on_disconnected(self) -> None:
        if self.up:
            log.info("the tool disconnected")
        self._mark_down()

    def _on_error(self, _err) -> None:
        if self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            return
        self._mark_down()

    def _on_ready(self) -> None:
        self._buf += bytes(self.sock.readAll().data())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._handle_line(line)
        if len(self._buf) > MAX_CTL_BUFFER:
            log.warning("message from the tool too long — disconnecting")
            self.sock.abort()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            log.warning("invalid message from the tool")
            return
        if not isinstance(msg, dict):
            return
        if "status" in msg:
            st = normalize_status(msg.get("status"))
            if st is not None:
                self.up = True
                self.status.emit(st)
        if msg.get("type") == "reply":
            self.reply.emit(msg)


class Plugin(QObject):
    def __init__(self, port: int, uuid: str, register_event: str = "registerPlugin",
                 env: dict | None = None, quit_cb=None) -> None:
        super().__init__()
        self.port = port
        self.uuid = uuid
        self.register_event = register_event or "registerPlugin"
        self.env = dict(os.environ if env is None else env)
        # Same language choice as the tool (WARPIPE_LANG, its config, the locale);
        # resolved once at start-up, also passed to the property inspector.
        self.lang = set_language(resolve_language(self.env))
        self.quit_cb = quit_cb or (lambda code=0: QGuiApplication.exit(code))
        self.contexts: dict[str, dict] = {}
        self.pi_visible: set[str] = set()
        self.global_settings: dict = {"toolPath": ""}
        self.status: dict | None = None
        self.tool_running = False
        self.ctl_known = False
        self.starting_until = 0.0
        self._last_launch = -1e9
        self._pending: dict[str, str] = {}
        self._next_id = 1
        self._ws_was_connected = False
        self.renderer = Renderer()

        self.ws = QWebSocket()
        self.ws.connected.connect(self._on_ws_connected)
        self.ws.disconnected.connect(self._on_ws_disconnected)
        self.ws.textMessageReceived.connect(self._on_ws_message)
        err_signal = getattr(self.ws, "errorOccurred", None) or getattr(self.ws, "error", None)
        if err_signal is not None:
            err_signal.connect(self._on_ws_error)

        self.ctl = ControlClient(control_socket_path(self.env))
        self.ctl.status.connect(self._on_status)
        self.ctl.reply.connect(self._on_reply)

        self._starting_timer = QTimer(self)
        self._starting_timer.setSingleShot(True)
        self._starting_timer.timeout.connect(self._starting_over)
        self._known_timer = QTimer(self)
        self._known_timer.setSingleShot(True)
        self._known_timer.timeout.connect(self._mark_known)

    # ------------------------------------------------------------- start
    def start(self) -> None:
        self.ctl.start()
        self._known_timer.start(FIRST_STATUS_WAIT_MS)
        self.ws.open(QUrl(f"ws://127.0.0.1:{int(self.port)}"))

    @property
    def starting(self) -> bool:
        return not self.tool_running and time.monotonic() < self.starting_until

    # ------------------------------------------------------------- sending
    def _send(self, obj: dict) -> None:
        self.ws.sendTextMessage(json.dumps(obj, ensure_ascii=False))

    def log_message(self, text: str) -> None:
        log.info("%s", text)
        self._send({"event": "logMessage", "payload": {"message": f"[Warpipe] {text}"}})

    def show_alert(self, ctx: str) -> None:
        self._send({"event": "showAlert", "context": ctx})

    def update(self, ctx: str) -> None:
        c = self.contexts.get(ctx)
        if c is None or not self.ctl_known:
            return
        v = visual_for(c["action"], c["settings"], self.status, self.tool_running, self.starting)
        url = self.renderer.url(v)
        if url != c.get("image"):
            c["image"] = url
            c["visual"] = v
            self._send({"event": "setImage", "context": ctx, "payload": {"image": url, "target": 0}})

    def refresh_all(self) -> None:
        for ctx in list(self.contexts):
            self.update(ctx)

    def push_pi(self, ctx: str) -> None:
        c = self.contexts.get(ctx)
        if c is None or c["action"] != ACTION_APP:
            return
        self._send({"event": "sendToPropertyInspector", "context": ctx, "action": c["action"],
                    "payload": apps_for_pi(self.status, self.tool_running, c["settings"])})

    # ------------------------------------------------------------- the tool
    def _mark_known(self) -> None:
        if not self.ctl_known:
            self.ctl_known = True
            self.refresh_all()

    def _on_status(self, st) -> None:
        self.status = st
        self.tool_running = st is not None
        if st is not None:
            self.starting_until = 0.0
            self._starting_timer.stop()
            self.ctl.retry_ms = RETRY_MS
        self.ctl_known = True
        self.refresh_all()
        for ctx in list(self.pi_visible):
            self.push_pi(ctx)

    def _on_reply(self, msg: dict) -> None:
        rid = msg.get("id")
        ctx = self._pending.pop(rid, None) if isinstance(rid, str) else None
        if not msg.get("ok"):
            err = msg.get("error") or tr("unknown error")
            if ctx is not None:
                self.show_alert(ctx)
            self.log_message(tr("Warpipe reported an error: {error}").format(error=err))
        elif msg.get("message"):
            log.info("tool: %s", msg["message"])

    def _command(self, cmd: dict, ctx: str) -> None:
        rid = f"p{self._next_id}"
        self._next_id += 1
        cmd = dict(cmd, id=rid)
        if not self.ctl.send(cmd):
            self.show_alert(ctx)
            self.log_message(tr("not connected to Warpipe"))
            return
        self._pending[rid] = ctx
        if len(self._pending) > 100:
            self._pending.pop(next(iter(self._pending)))

    def _starting_over(self) -> None:
        self.ctl.retry_ms = RETRY_MS
        self.refresh_all()

    def launch_tool(self, ctx: str) -> None:
        argv, problem = resolve_tool(self.global_settings, self.env)
        if argv is None:
            self.show_alert(ctx)
            self.log_message(problem)
            return
        now = time.monotonic()
        if now - self._last_launch < LAUNCH_COOLDOWN_S:
            return
        self._last_launch = now
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
                cwd=self.env.get("HOME") or os.path.expanduser("~"), env=child_env(self.env))
        except OSError as e:
            self.show_alert(ctx)
            self.log_message(tr("failed to start {program}: {error}").format(program=argv[0], error=e))
            return
        # reap the exit status in the background (no zombie); the process runs in its own session
        threading.Thread(target=proc.wait, name="warpipe_plugin-reaper", daemon=True).start()
        self.log_message(tr("starting Warpipe: {program} --tray").format(program=argv[0]))
        self.starting_until = now + STARTING_S
        self._starting_timer.start(int(STARTING_S * 1000) + 50)
        self.ctl.retry_ms = RETRY_STARTING_MS
        self.ctl.retry_soon(RETRY_STARTING_MS)
        self.refresh_all()

    # ------------------------------------------------------------- OpenDeck
    def _on_ws_connected(self) -> None:
        self._ws_was_connected = True
        self._send({"event": self.register_event, "uuid": self.uuid})
        self._send({"event": "getGlobalSettings", "context": self.uuid})
        log.info("registered with OpenDeck as %s (plugin %s, language %s)", self.uuid, __version__, self.lang)

    def _on_ws_disconnected(self) -> None:
        log.info("OpenDeck closed the connection — exiting")
        self.quit_cb(0)

    def _on_ws_error(self, _err=None) -> None:
        from PySide6.QtNetwork import QAbstractSocket
        if self.ws.state() == QAbstractSocket.SocketState.UnconnectedState and not self._ws_was_connected:
            log.error("can't connect to OpenDeck (port %s): %s", self.port, self.ws.errorString())
            self.quit_cb(1)

    def _ensure_ctx(self, ctx: str, action: str, settings=None) -> dict:
        c = self.contexts.get(ctx)
        if c is None:
            c = {"action": action, "settings": sanitize_settings(settings), "image": None}
            self.contexts[ctx] = c
        elif settings is not None:
            c["settings"] = sanitize_settings(settings)
        return c

    def _on_ws_message(self, text: str) -> None:
        try:
            self._dispatch(text)
        except Exception:
            log.exception("error while handling an event")

    def _dispatch(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        ev = msg.get("event")
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        if ev == "didReceiveGlobalSettings":
            self.global_settings = sanitize_global_settings(payload.get("settings"))
            return
        ctx = msg.get("context")
        action = msg.get("action")
        if not isinstance(ctx, str) or not ctx or len(ctx) > MAX_CONTEXT_LEN:
            return
        if ev in ("willAppear", "willDisappear", "keyDown", "keyUp", "didReceiveSettings",
                  "propertyInspectorDidAppear", "propertyInspectorDidDisappear", "sendToPlugin") \
                and action not in ACTIONS and ctx not in self.contexts:
            return
        if ev == "willAppear":
            self.contexts.pop(ctx, None)
            self._ensure_ctx(ctx, action, payload.get("settings"))
            self.update(ctx)
        elif ev == "willDisappear":
            self.contexts.pop(ctx, None)
            self.pi_visible.discard(ctx)
        elif ev == "didReceiveSettings":
            self._ensure_ctx(ctx, action or self.contexts.get(ctx, {}).get("action"), payload.get("settings"))
            self.update(ctx)
            if ctx in self.pi_visible:
                self.push_pi(ctx)
        elif ev == "keyDown":
            c = self._ensure_ctx(ctx, action, payload.get("settings"))
            self._press(ctx, c)
        elif ev == "propertyInspectorDidAppear":
            if ctx in self.contexts:
                self.pi_visible.add(ctx)
                self.push_pi(ctx)
        elif ev == "propertyInspectorDidDisappear":
            self.pi_visible.discard(ctx)
        elif ev == "sendToPlugin":
            if payload.get("cmd") == "listApps" and ctx in self.contexts:
                self.push_pi(ctx)

    def _press(self, ctx: str, c: dict) -> None:
        if not self.ctl_known:
            # first fraction of a second after start: we don't know yet whether the tool
            # runs — don't start it blindly (a second instance could pop up)
            log.info("key press ignored: still checking whether the tool is running")
            return
        p = press_for(c["action"], c["settings"], self.status, self.tool_running, self.starting)
        if p.kind == "launch":
            self.launch_tool(ctx)
        elif p.kind == "toggle":
            self._command({"cmd": "toggle", "app": p.app}, ctx)
        elif p.kind == "system":
            self._command({"cmd": "system"}, ctx)
        elif p.kind == "alert":
            self.show_alert(ctx)
            self.log_message(p.reason)


# --------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="warpipe-plugin", allow_abbrev=False,
                                 description="Warpipe OpenDeck/Stream Deck plugin "
                                             "(started by OpenDeck).")
    ap.add_argument("-port", type=int, required=True)
    ap.add_argument("-pluginUUID", required=True)
    ap.add_argument("-registerEvent", default="registerPlugin")
    ap.add_argument("-info", default="{}")
    args, _unknown = ap.parse_known_args(argv)
    return args


def _qpa_platform() -> str | None:
    """The plugin shows no windows: offscreen (or minimal) if available."""
    try:
        plugins = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
    except Exception:  # pragma: no cover
        return None
    for name in ("offscreen", "minimal"):
        if os.path.exists(os.path.join(plugins, "platforms", f"libq{name}.so")):
            return name
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s warpipe-plugin %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        info = json.loads(args.info)
        app_info = info.get("application", {}) if isinstance(info, dict) else {}
        log.info("start: %s %s, platform %s", app_info.get("platform", "?"),
                 app_info.get("version", "?"), sys.platform)
    except ValueError:
        pass
    # -platform in argv (not an environment variable), so the tray icon we may
    # start does not inherit "offscreen".
    qargv = ["warpipe-plugin"]
    plat = _qpa_platform()
    if plat:
        qargv += ["-platform", plat]
    app = QGuiApplication(qargv)
    app.setApplicationName("warpipe-plugin")
    plugin = Plugin(args.port, args.pluginUUID, args.registerEvent)
    plugin.start()
    return app.exec()
