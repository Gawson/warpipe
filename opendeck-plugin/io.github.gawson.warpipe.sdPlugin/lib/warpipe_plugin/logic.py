"""Czysta logika wtyczki (bez Qt, bez sieci): łatwa do testowania.

Słowniczek:
  status — stan z narzędzia (gniazdo sterowania), np.
           {"enforce": false, "capture": {"status": "active", "count": 1},
            "apps": [{"key": "Firefox", "display": "Firefox", "present": true,
                      "selected": false, "in_stream": true}]}
           None = narzędzie nie działa (brak połączenia).
  settings — ustawienia jednego przycisku w OpenDeck: {"app": klucz, "label": podpis}
  global settings — ustawienia całej wtyczki: {"toolPath": ścieżka do warpipe}
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass

ACTION_APP = "io.github.gawson.warpipe.app"
ACTION_SYSTEM = "io.github.gawson.warpipe.system"
TOOL_NAME = "warpipe"
MAX_SETTING_LEN = 200

# Stany przycisku
S_UNSET = "unset"          # przycisk bez wybranej aplikacji
S_DOWN = "down"            # narzędzie nie działa (naciśnięcie je uruchamia)
S_STARTING = "starting"    # właśnie uruchamiamy narzędzie
S_ERROR = "error"          # narzędzie działa, ale zgłasza błąd (np. PipeWire)
S_ABSENT = "absent"        # aplikacja teraz nie gra
S_NOSTREAM = "nostream"    # aplikacja gra, ale nie ma streamu z dźwiękiem
S_UNSURE = "unsure"        # stream jest, ale narzędzie nie wie, który strumień Discorda
S_OFF = "off"              # aplikacja gra, nie idzie do streamu
S_ON = "on"                # aplikacja gra i JEST w streamie
S_SYS_ON = "sys_on"        # „Cały system” włączony (pilnowanie wyłączone)
S_SYS_OFF = "sys_off"      # tylko wybrane aplikacje

BADGE = {
    S_UNSET: "",
    S_DOWN: "uruchom",
    S_STARTING: "uruchamiam…",
    S_ERROR: "błąd",
    S_ABSENT: "brak",
    S_NOSTREAM: "brak streamu",
    S_UNSURE: "niepewny",
    S_OFF: "",
    S_ON: "na streamie",
    S_SYS_ON: "włączony",
    S_SYS_OFF: "tylko wybrane",
}

SYSTEM_LABEL = "Cały system"
UNSET_LABEL = "wybierz aplikację"

# Zmienne środowiska, których NIE przekazujemy uruchamianemu narzędziu (mogłyby
# podmienić Pythona albo biblioteki, np. gdy OpenDeck działa z AppImage).
CHILD_ENV_DROP = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH",
                  "PYTHONUSERBASE", "LD_LIBRARY_PATH", "LD_PRELOAD")


# --------------------------------------------------------------------------
# Ścieżki
# --------------------------------------------------------------------------

def runtime_dir(env=None) -> str:
    """To samo co warpipe.state.runtime_dir()."""
    env = os.environ if env is None else env
    d = env.get("DISCORD_DZWIEK_RUNTIME_DIR")
    if d:
        return d
    base = env.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return os.path.join(base, "warpipe")
    return os.path.join(tempfile.gettempdir(), f"warpipe-{os.getuid()}")


def control_socket_path(env=None) -> str:
    return os.path.join(runtime_dir(env), "control.sock")


# --------------------------------------------------------------------------
# Ustawienia
# --------------------------------------------------------------------------

def clean_text(value, limit: int = MAX_SETTING_LEN) -> str:
    """Tekst z ustawień: tylko str, bez znaków sterujących, przycięty."""
    if not isinstance(value, str):
        return ""
    v = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127).strip()
    return v[:limit].strip()


def sanitize_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"app": clean_text(raw.get("app")), "label": clean_text(raw.get("label"), 60)}


def sanitize_global_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"toolPath": clean_text(raw.get("toolPath"), 4096)}


def short_label(key: str) -> str:
    """Krótszy podpis: „Warframe.x64.exe” → „Warframe”."""
    s = (key or "").strip()
    low = s.lower()
    if low.endswith(".exe") and len(s) > 4:
        s, low = s[:-4], low[:-4]
    for suf in (".x64", "_x64", "-x64", ".x86_64", "_x86_64", ".x86", "_x86", "-win64", "_win64", ".win64"):
        if low.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s or (key or "").strip()


def label_for(settings: dict) -> str:
    return settings.get("label") or short_label(settings.get("app", ""))


# --------------------------------------------------------------------------
# Stan narzędzia
# --------------------------------------------------------------------------

def normalize_status(raw) -> dict | None:
    """Sprawdza kształt stanu z gniazda; zły kształt → None (traktujemy jak „brak stanu”)."""
    if not isinstance(raw, dict):
        return None
    cap = raw.get("capture") if isinstance(raw.get("capture"), dict) else {}
    cs = cap.get("status") if cap.get("status") in ("active", "none", "ambiguous") else "none"
    count = cap.get("count") if isinstance(cap.get("count"), int) and not isinstance(cap.get("count"), bool) else 0
    apps = []
    for a in raw.get("apps") if isinstance(raw.get("apps"), list) else []:
        if not isinstance(a, dict) or not isinstance(a.get("key"), str) or not a["key"]:
            continue
        apps.append({
            "key": a["key"][:MAX_SETTING_LEN],
            "display": (a.get("display") if isinstance(a.get("display"), str) and a.get("display") else a["key"])[:MAX_SETTING_LEN],
            "present": a.get("present") is True,
            "selected": a.get("selected") is True,
            "in_stream": a.get("in_stream") is True,
        })
    out = {"enforce": raw.get("enforce") is True, "capture": {"status": cs, "count": count}, "apps": apps}
    if isinstance(raw.get("error"), str) and raw["error"]:
        out["error"] = raw["error"][:500]
    return out


def find_app(status: dict | None, key: str) -> dict | None:
    if not status or not key:
        return None
    cf = key.casefold()
    for a in status.get("apps", []):
        if a["key"].casefold() == cf:
            return a
    return None


def is_effective(status: dict | None, key: str) -> bool:
    """Aplikacja jest wybrana „na wyłączność” (pilnowanie włączone i zaznaczona)."""
    a = find_app(status, key)
    return bool(status and status.get("enforce") and a and a.get("selected"))


# --------------------------------------------------------------------------
# Wygląd przycisku
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Visual:
    kind: str           # "app" | "system"
    state: str          # S_*
    label: str          # podpis (nazwa aplikacji)
    badge: str          # mały napis u góry
    ring: bool = False  # zielona ramka: zaznaczona, ale teraz nie w streamie

    @property
    def tag(self) -> str:
        return f"{self.kind}:{self.state}"


def _v(kind: str, state: str, label: str, ring: bool = False) -> Visual:
    return Visual(kind, state, label, BADGE[state], ring)


def app_visual(settings: dict, status: dict | None, tool_running: bool, starting: bool = False) -> Visual:
    key = settings.get("app", "")
    if not key:
        return _v("app", S_UNSET, UNSET_LABEL)
    label = label_for(settings)
    if not tool_running or status is None:
        return _v("app", S_STARTING if starting else S_DOWN, label)
    if status.get("error"):
        return _v("app", S_ERROR, label)
    a = find_app(status, key)
    ring = is_effective(status, key)
    if a is None or not a.get("present"):
        return _v("app", S_ABSENT, label, ring)
    if a.get("in_stream"):
        return _v("app", S_ON, label)
    cap = status.get("capture", {}).get("status")
    if cap == "none":
        return _v("app", S_NOSTREAM, label, ring)
    if cap == "ambiguous":
        return _v("app", S_UNSURE, label, ring)
    return _v("app", S_OFF, label, ring)


def system_visual(status: dict | None, tool_running: bool, starting: bool = False) -> Visual:
    if not tool_running or status is None:
        return _v("system", S_STARTING if starting else S_DOWN, SYSTEM_LABEL)
    if status.get("enforce"):
        return _v("system", S_SYS_OFF, SYSTEM_LABEL)
    return _v("system", S_SYS_ON, SYSTEM_LABEL)


def visual_for(action: str, settings: dict, status: dict | None, tool_running: bool,
               starting: bool = False) -> Visual:
    if action == ACTION_SYSTEM:
        return system_visual(status, tool_running, starting)
    return app_visual(settings, status, tool_running, starting)


# --------------------------------------------------------------------------
# Naciśnięcie
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Press:
    kind: str          # "launch" | "toggle" | "system" | "alert" | "none"
    app: str = ""
    reason: str = ""


def press_for(action: str, settings: dict, status: dict | None, tool_running: bool,
              starting: bool = False) -> Press:
    if action == ACTION_SYSTEM:
        if not tool_running or status is None:
            return Press("none") if starting else Press("launch")
        return Press("system")
    key = settings.get("app", "")
    if not key:
        return Press("alert", reason="Przycisk nie ma wybranej aplikacji — wybierz ją w ustawieniach przycisku.")
    if not tool_running or status is None:
        return Press("none") if starting else Press("launch")
    a = find_app(status, key)
    if (a is None or not a.get("present")) and not is_effective(status, key):
        return Press("alert", reason=f"Aplikacja {key} teraz nie gra — nie ma czego przełączać.")
    return Press("toggle", app=key)


# --------------------------------------------------------------------------
# Inspektor właściwości (lista aplikacji)
# --------------------------------------------------------------------------

def apps_for_pi(status: dict | None, tool_running: bool, settings: dict) -> dict:
    apps = []
    if status:
        for a in status.get("apps", []):
            apps.append({k: a[k] for k in ("key", "display", "present", "selected", "in_stream")})
        apps.sort(key=lambda a: (not a["present"], a["display"].casefold()))
    return {
        "type": "apps",
        "toolRunning": bool(tool_running and status is not None),
        "capture": (status or {}).get("capture", {}).get("status", ""),
        "enforce": bool(status and status.get("enforce")),
        "apps": apps,
        "current": dict(settings),
    }


# --------------------------------------------------------------------------
# Uruchamianie narzędzia
# --------------------------------------------------------------------------

def _is_exec_file(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(path, os.X_OK)


def resolve_tool(global_settings: dict, env=None, which=shutil.which) -> tuple[list[str] | None, str]:
    """Polecenie uruchamiające ikonę narzędzia: (argv, opis problemu).

    Zawsze lista argumentów (bez powłoki). Kolejność: ścieżka z ustawień wtyczki,
    warpipe z PATH, ~/.local/bin, ~/.local/share/warpipe."""
    env = os.environ if env is None else env
    configured = (global_settings or {}).get("toolPath", "")
    home = env.get("HOME") or os.path.expanduser("~")
    if configured:
        p = configured
        if p.startswith("~/"):
            p = os.path.join(home, p[2:])
        if not os.path.isabs(p):
            return None, f"Ścieżka do programu musi być pełna (zaczynać się od / albo ~/): {configured}"
        if not _is_exec_file(p):
            return None, f"Nie ma programu (albo nie da się go uruchomić): {p}"
        return [p, "--tray"], ""
    # Tylko bezwzględne wpisy PATH: „.” albo puste wpisy znaczyłyby katalog bieżący
    # (a OpenDeck uruchamia wtyczkę w jej własnym katalogu).
    path = os.pathsep.join(p for p in (env.get("PATH") or "").split(os.pathsep) if os.path.isabs(p))
    found = which(TOOL_NAME, path=path) if path else None
    if found and os.path.isabs(found) and _is_exec_file(found):
        return [found, "--tray"], ""
    for p in (os.path.join(home, ".local", "bin", TOOL_NAME),
              os.path.join(home, ".local", "share", "warpipe", TOOL_NAME)):
        if _is_exec_file(p):
            return [p, "--tray"], ""
    return None, ("Nie znalazłem programu warpipe (ani w PATH, ani w ~/.local/bin). "
                  "Zainstaluj go albo podaj ścieżkę w ustawieniach przycisku (Zaawansowane).")


def child_env(env=None) -> dict:
    env = dict(os.environ if env is None else env)
    for k in CHILD_ENV_DROP:
        env.pop(k, None)
    return env
