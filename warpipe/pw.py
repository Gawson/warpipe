"""Adapter do narzędzi PipeWire (pw-dump, pw-link).

Połączenia tworzymy i usuwamy po numerycznych id portów z pw-dump — nazwy
„węzeł:port” nie są unikalne (np. kilka węzłów discord_capture).

pw-link -L (linger) jest domyślne: połączenie żyje dalej po zakończeniu pw-link
i znika samo, gdy zniknie którykolwiek z portów. Nasze połączenia znakujemy
właściwością MANAGED_KEY (pw-link -p), żeby dało się je później znaleźć.
Odtworzone oryginały (z migawki) dostają RESTORED_KEY — przywracanie ich nie
usuwa, ale wiemy, że to my je utworzyliśmy.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .graph import MANAGED_KEY, RESTORED_KEY


class PwError(Exception):
    """Błąd narzędzi PipeWire. Komunikat jest po polsku, dla użytkownika."""


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class PwCli:
    def __init__(self, timeout: float = 8.0, owner_value: str = "true") -> None:
        self.timeout = timeout
        self.owner_value = owner_value

    def _run(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args, capture_output=True, text=True,
                timeout=timeout or self.timeout, check=False,
            )
        except FileNotFoundError as e:
            raise PwError(f"Nie znaleziono programu {args[0]} — czy PipeWire (pipewire-utils) jest zainstalowany?") from e
        except subprocess.TimeoutExpired as e:
            raise PwError(f"{args[0]} nie odpowiedział w {timeout or self.timeout:.0f} s — czy PipeWire działa?") from e
        except OSError as e:
            raise PwError(f"Nie mogę uruchomić {args[0]}: {e}") from e

    # ------------------------------------------------------------------ odczyt
    def dump(self) -> list[dict]:
        r = self._run(["pw-dump", "-N"])
        if r.returncode != 0:
            raise PwError(f"Nie mogę odczytać stanu PipeWire (pw-dump, kod {r.returncode}): {_short(r.stderr)}")
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise PwError("pw-dump zwrócił niepoprawne dane — czy PipeWire działa?") from e
        if not isinstance(data, list):
            raise PwError("pw-dump zwrócił nieoczekiwany format danych.")
        return data

    # ------------------------------------------------------------------ zmiany
    def link(self, out_port: int, in_port: int, managed: bool = True) -> str:
        """Tworzy połączenie. Zwraca 'created' albo 'exists'.

        managed=True: nasze dodatkowe połączenie (usuwane przy przywracaniu);
        managed=False: odtworzony oryginał (zostaje po przywróceniu)."""
        args = ["pw-link", "-L"]
        key = MANAGED_KEY if managed else RESTORED_KEY
        args += ["-p", json.dumps({key: self.owner_value})]
        args += [str(int(out_port)), str(int(in_port))]
        r = self._run(args)
        if r.returncode == 0:
            return "created"
        if "File exists" in (r.stderr or "") or "File exists" in (r.stdout or ""):
            return "exists"
        raise PwError(f"pw-link nie połączył portów {out_port} -> {in_port}: {_short(r.stderr or r.stdout)}")

    def unlink(self, out_port: int, in_port: int) -> str:
        """Usuwa połączenie między dwoma portami. Zwraca 'deleted' albo 'missing'."""
        r = self._run(["pw-link", "-d", str(int(out_port)), str(int(in_port))])
        if r.returncode == 0:
            return "deleted"
        err = (r.stderr or "") + (r.stdout or "")
        if "No such file" in err or "not found" in err.lower():
            return "missing"
        raise PwError(f"pw-link nie rozłączył portów {out_port} -> {in_port}: {_short(err)}")

    # ---------------------------------------------------------------- monitor
    def start_monitor(self) -> Any:
        """Uruchamia `pw-dump -m -N` (strumień JSON ze zmianami grafu)."""
        try:
            return subprocess.Popen(
                ["pw-dump", "-m", "-N"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except (FileNotFoundError, OSError) as e:
            raise PwError(f"Nie mogę uruchomić pw-dump -m: {e}") from e
