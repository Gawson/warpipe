"""Config, link snapshot, the single-instance lock and start at login.

Config: ~/.config/warpipe/config.json (or $WARPIPE_CONFIG_DIR).
Start at login: ~/.config/autostart/io.github.gawson.Warpipe.desktop, a copy of the
installed desktop entry (the same file install.sh --autostart writes).
Snapshot and lock: $XDG_RUNTIME_DIR/warpipe/ (or $WARPIPE_RUNTIME_DIR).
The snapshot lives in the runtime directory (tmpfs) because it describes the
current PipeWire graph — it is meaningless after logging out anyway.

media.name is never stored here (tab/track titles may be private).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from . import DESKTOP_ID
from .i18n import _
from .planner import (DEFAULT_CAPTURE_NAMES, DEFAULT_MATCH_TERMS,
                      DEFAULT_MIC_NAMES, DetectConfig)

CONFIG_VERSION = 1


def config_dir() -> Path:
    env = os.environ.get("WARPIPE_CONFIG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "warpipe"


def runtime_dir() -> Path:
    env = os.environ.get("WARPIPE_RUNTIME_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "warpipe"
    return Path(tempfile.gettempdir()) / f"warpipe-{os.getuid()}"


def _atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write(f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    def write(f):
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    _atomic_write(path, write)


# ------------------------------------------------------------ start at login
AUTOSTART_EXTRA = "X-KDE-autostart-phase=2"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.join(os.path.expanduser("~"), default))


def autostart_file() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{DESKTOP_ID}.desktop"


def installed_desktop_file() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "applications" / f"{DESKTOP_ID}.desktop"


def autostart_enabled() -> bool:
    try:
        text = autostart_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "hidden=true" not in text.replace(" ", "").lower()


def autostart_available() -> bool:
    """Start at login can be switched on: Warpipe is installed (install.sh)."""
    return installed_desktop_file().is_file()


def set_autostart(on: bool) -> None:
    """Copy the installed desktop entry to the autostart folder, or remove it.
    Raises OSError (e.g. not installed)."""
    path = autostart_file()
    if not on:
        path.unlink(missing_ok=True)
        return
    text = installed_desktop_file().read_text(encoding="utf-8")
    if AUTOSTART_EXTRA not in text:
        text = text.rstrip("\n") + "\n" + AUTOSTART_EXTRA + "\n"
    _atomic_write(path, lambda f: f.write(text))


def is_utf8_text(value) -> bool:
    """A str that can be written as UTF-8 (no lone surrogates, e.g. "\\ud800").

    JSON allows such escapes, but a name containing one can't be saved, sent
    over the control socket or printed — it is treated as invalid everywhere."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _str_list(value, default: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return tuple(x for x in value if x.strip())
    return tuple(default)


class Config:
    """User settings. Unknown keys are kept when saving."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_dir() / "config.json")
        self.raw: dict = {}
        self.load_error: str | None = None
        self.load()

    def load(self) -> None:
        self.load_error = None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.raw = data
            else:
                self.raw = {}
                self.load_error = _("{path}: invalid format (a JSON object was expected)").format(path=self.path)
        except FileNotFoundError:
            self.raw = {}
        except (OSError, ValueError) as e:  # ValueError: also UnicodeDecodeError and JSONDecodeError
            self.raw = {}
            self.load_error = _("Can't read {path}: {error}").format(path=self.path, error=e)

    def save(self) -> None:
        self.raw["version"] = CONFIG_VERSION
        _atomic_write_json(self.path, self.raw)

    # --------------------------------------------------------------- fields
    @property
    def selected_apps(self) -> list[str]:
        v = self.raw.get("selected_apps")
        if isinstance(v, list):
            # a hand-edited "\ud800" would make every later save fail — skip it
            return [x for x in v if isinstance(x, str) and x and is_utf8_text(x)]
        return []

    @selected_apps.setter
    def selected_apps(self, keys: Iterable[str]) -> None:
        seen = {}
        for k in keys:
            if k and is_utf8_text(k) and k.casefold() not in seen:
                seen[k.casefold()] = k
        self.raw["selected_apps"] = sorted(seen.values(), key=str.casefold)

    @property
    def enforce(self) -> bool:
        # Only real JSON true/false; "false" (a string) does NOT mean enabled.
        v = self.raw.get("enforce", False)
        return v if isinstance(v, bool) else False

    @enforce.setter
    def enforce(self, value: bool) -> None:
        self.raw["enforce"] = bool(value)

    @property
    def language(self) -> str:
        """UI language setting: "auto" (default), "en" or "pl" (see i18n)."""
        v = self.raw.get("language")
        return v if isinstance(v, str) and v.strip() else "auto"

    @language.setter
    def language(self, value: str) -> None:
        self.raw["language"] = value if value in ("en", "pl") else "auto"

    @property
    def confirmed_captures(self) -> list[tuple[str, str, str]]:
        """"Monitor-only" streams confirmed by the user as stream audio."""
        v = self.raw.get("confirmed_captures")
        out: list[tuple[str, str, str]] = []
        if isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and all(isinstance(x.get(k), str) for k in ("program", "node", "sink")):
                    t = (x["program"], x["node"], x["sink"])
                    if t not in out:
                        out.append(t)
        return out

    def add_confirmed(self, sigs: Iterable[tuple[str, str, str]]) -> bool:
        cur = self.confirmed_captures
        new = [tuple(s) for s in sigs if tuple(s) not in cur]
        if not new:
            return False
        self.raw["confirmed_captures"] = [{"program": p, "node": n, "sink": s}
                                          for p, n, s in dict.fromkeys(cur + new)]
        return True

    def detect_config(self) -> DetectConfig:
        return DetectConfig(
            match_terms=_str_list(self.raw.get("capture_match_terms"), DEFAULT_MATCH_TERMS),
            capture_names=_str_list(self.raw.get("capture_names"), DEFAULT_CAPTURE_NAMES),
            mic_names=_str_list(self.raw.get("mic_names"), DEFAULT_MIC_NAMES),
            confirmed=tuple(self.confirmed_captures),
        )


SNAPSHOT_VERSION = 2


def _empty_snapshot(instance: str | None = None) -> dict:
    return {"version": SNAPSHOT_VERSION, "pw_instance": instance, "captures": {}}


def _sanitize_snapshot(d) -> dict:
    """Only the valid shape: captures = {"<serial>": {"node_name": str, "links": [dict, ...]}}.
    Bad entries are skipped (they must not break restore or enforcement)."""
    if not isinstance(d, dict):
        return _empty_snapshot()
    inst = d.get("pw_instance")
    out = _empty_snapshot(inst if isinstance(inst, str) else None)
    caps = d.get("captures")
    if not isinstance(caps, dict):
        return out
    for k, v in caps.items():
        if not (isinstance(k, str) and k.isdigit() and isinstance(v, dict)):
            continue
        name = v.get("node_name")
        links = v.get("links")
        out["captures"][k] = {
            "node_name": name if isinstance(name, str) else None,
            "links": [dict(e) for e in links if isinstance(e, dict)] if isinstance(links, list) else [],
        }
    return out


class SnapshotStore:
    """Snapshot of the links going into capture nodes (key: the node's object.serial).

    Format:
      {"version": 2, "pw_instance": "<daemon cookie:pid>",
       "captures": {"<serial>": {"node_name": str, "links": [entry, ...]}}}
    Entry (planner.link_entry): port/node serials, node and port names,
    "deleted": whether we removed it (only those are recreated on restore).
    pw_instance: serial is a PipeWire daemon counter — after a daemon restart the
    snapshot is worthless (and dangerous), so it is then discarded entirely.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (runtime_dir() / "snapshot.json")
        self.data: dict = _empty_snapshot()
        self.load_error: str | None = None
        self.load()

    def load(self) -> None:
        self.load_error = None
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.data = _sanitize_snapshot(d)
        except FileNotFoundError:
            self.data = _empty_snapshot()
        except (OSError, ValueError) as e:  # ValueError: also UnicodeDecodeError and JSONDecodeError
            self.data = _empty_snapshot()
            self.load_error = _("Can't read the snapshot {path}: {error}").format(path=self.path, error=e)

    @property
    def instance(self) -> str | None:
        return self.data.get("pw_instance")

    def check_instance(self, instance: str | None) -> bool:
        """Bind the snapshot to the current PipeWire session.

        Returns True if the snapshot was from another session and was discarded
        (then it has to be saved, to delete the file)."""
        if self.data.get("pw_instance") == instance:
            return False
        had = bool(self.captures)
        self.data = _empty_snapshot(instance)
        return had

    def save(self) -> None:
        if not self.data.get("captures"):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        _atomic_write_json(self.path, self.data)

    @property
    def captures(self) -> dict:
        return self.data.setdefault("captures", {})

    def is_empty(self) -> bool:
        return not any((c.get("links") for c in self.captures.values()))

    def ensure_capture(self, serial: int, node_name: str, entries: list[dict]) -> None:
        """Store the initial state of a node unless we already have it."""
        key = str(serial)
        if key in self.captures:
            return
        self.captures[key] = {"node_name": node_name, "links": [dict(e) for e in entries]}

    def mark_deleted(self, serial: int, entry: dict, node_name: str | None = None) -> None:
        cap = self.captures.setdefault(str(serial), {"node_name": node_name, "links": []})
        if cap.get("node_name") is None and node_name is not None:
            cap["node_name"] = node_name
        for e in cap["links"]:
            if (e.get("out_port_serial"), e.get("in_port_serial")) == (entry.get("out_port_serial"), entry.get("in_port_serial")):
                e["deleted"] = True
                return
        e = dict(entry)
        e["deleted"] = True
        cap["links"].append(e)

    def mark_restored(self, serial: int, out_port_serial: int, in_port_serial: int) -> bool:
        """A snapshot link exists again (created by us WITHOUT the marker)."""
        cap = self.captures.get(str(serial))
        if not cap:
            return False
        for e in cap["links"]:
            if (e.get("out_port_serial"), e.get("in_port_serial")) == (out_port_serial, in_port_serial):
                changed = e.get("deleted") is not False
                e["deleted"] = False
                return changed
        return False

    def prune(self, existing_serials: Iterable[int]) -> bool:
        keep = {str(s) for s in existing_serials}
        gone = [k for k in self.captures if k not in keep]
        for k in gone:
            del self.captures[k]
        return bool(gone)

    def clear(self) -> None:
        self.data = _empty_snapshot(self.data.get("pw_instance"))


class InstanceLock:
    """Single-instance lock (flock in the runtime directory)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (runtime_dir() / "instance.lock")
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def holder_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip() or "0") or None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
