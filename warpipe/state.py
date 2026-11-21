"""Konfiguracja, migawka połączeń i blokada pojedynczej instancji.

Konfiguracja: ~/.config/warpipe/config.json (albo $DISCORD_DZWIEK_CONFIG_DIR).
Migawka i blokada: $XDG_RUNTIME_DIR/warpipe/ (albo $DISCORD_DZWIEK_RUNTIME_DIR).
Migawka leży w katalogu runtime (tmpfs), bo dotyczy bieżącego grafu PipeWire —
po wylogowaniu i tak traci sens.

Nigdy nie zapisujemy tu media.name (tytuły kart/utworów mogą być prywatne).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .planner import (DEFAULT_CAPTURE_NAMES, DEFAULT_MATCH_TERMS,
                      DEFAULT_MIC_NAMES, DetectConfig)

CONFIG_VERSION = 1


def config_dir() -> Path:
    env = os.environ.get("DISCORD_DZWIEK_CONFIG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "warpipe"


def runtime_dir() -> Path:
    env = os.environ.get("DISCORD_DZWIEK_RUNTIME_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "warpipe"
    return Path(tempfile.gettempdir()) / f"warpipe-{os.getuid()}"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _str_list(value, default: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return tuple(x for x in value if x.strip())
    return tuple(default)


class Config:
    """Ustawienia użytkownika. Klucze nieznane są zachowywane przy zapisie."""

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
                self.load_error = f"{self.path}: zły format (oczekiwany obiekt JSON)"
        except FileNotFoundError:
            self.raw = {}
        except (OSError, ValueError) as e:  # ValueError: też UnicodeDecodeError i JSONDecodeError
            self.raw = {}
            self.load_error = f"Nie mogę odczytać {self.path}: {e}"

    def save(self) -> None:
        self.raw["version"] = CONFIG_VERSION
        _atomic_write_json(self.path, self.raw)

    # --------------------------------------------------------------- pola
    @property
    def selected_apps(self) -> list[str]:
        v = self.raw.get("selected_apps")
        if isinstance(v, list):
            return [x for x in v if isinstance(x, str) and x]
        return []

    @selected_apps.setter
    def selected_apps(self, keys: Iterable[str]) -> None:
        seen = {}
        for k in keys:
            if k and k.casefold() not in seen:
                seen[k.casefold()] = k
        self.raw["selected_apps"] = sorted(seen.values(), key=str.casefold)

    @property
    def enforce(self) -> bool:
        # Tylko prawdziwe true/false z JSON-a; "false" (tekst) to NIE włączone.
        v = self.raw.get("enforce", False)
        return v if isinstance(v, bool) else False

    @enforce.setter
    def enforce(self, value: bool) -> None:
        self.raw["enforce"] = bool(value)

    @property
    def confirmed_captures(self) -> list[tuple[str, str, str]]:
        """Strumienie „z monitora” potwierdzone przez użytkownika jako dźwięk streamu."""
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
    """Tylko poprawny kształt: captures = {"<serial>": {"node_name": str, "links": [dict, ...]}}.
    Złe wpisy są pomijane (nie mogą wywrócić przywracania ani pilnowania)."""
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
    """Migawka połączeń wchodzących do węzłów przechwytujących (klucz: object.serial węzła).

    Format:
      {"version": 2, "pw_instance": "<cookie:pid demona>",
       "captures": {"<serial>": {"node_name": str, "links": [wpis, ...]}}}
    Wpis (planner.link_entry): serial portów/węzłów, nazwy węzłów i portów,
    "deleted": czy to my go usunęliśmy (tylko takie odtwarzamy przy przywracaniu).
    pw_instance: serial to licznik demona PipeWire — po jego restarcie migawka
    jest bezwartościowa (i niebezpieczna), więc jest wtedy odrzucana w całości.
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
        except (OSError, ValueError) as e:  # ValueError: też UnicodeDecodeError i JSONDecodeError
            self.data = _empty_snapshot()
            self.load_error = f"Nie mogę odczytać migawki {self.path}: {e}"

    @property
    def instance(self) -> str | None:
        return self.data.get("pw_instance")

    def check_instance(self, instance: str | None) -> bool:
        """Wiąże migawkę z bieżącym uruchomieniem PipeWire.

        Zwraca True, jeśli migawka była z innego uruchomienia i została odrzucona
        (wtedy trzeba ją zapisać, żeby skasować plik)."""
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
        """Zapisuje stan początkowy węzła, jeśli jeszcze go nie mamy."""
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
        """Połączenie z migawki znowu istnieje (utworzone przez nas BEZ znacznika)."""
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
    """Blokada pojedynczej instancji (flock w katalogu runtime)."""

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
