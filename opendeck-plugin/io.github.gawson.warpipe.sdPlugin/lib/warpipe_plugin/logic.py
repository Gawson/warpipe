"""Pure plugin logic (no Qt, no network): easy to test.

Glossary:
  status — state reported by the tool (control socket), e.g.
           {"enforce": false, "capture": {"status": "active", "count": 1},
            "apps": [{"key": "Firefox", "display": "Firefox", "present": true,
                      "selected": false, "in_stream": true}]}
           None = the tool is not running (no connection).
  settings — settings of one OpenDeck key: {"app": key, "label": caption}
  global settings — plugin-wide settings: {"toolPath": path to warpipe}

Language: English by default, Polish as an optional translation (tr(), PL).
The choice mirrors the tool (warpipe/i18n.py): WARPIPE_LANG, then the tool's
config key "language" ("auto" | "en" | "pl"), then LC_ALL / LC_MESSAGES / LANG.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass

ACTION_APP = "io.github.gawson.warpipe.app"
ACTION_SYSTEM = "io.github.gawson.warpipe.system"
TOOL_NAME = "warpipe"
MAX_SETTING_LEN = 200

# Key states
S_UNSET = "unset"          # key without a chosen app
S_DOWN = "down"            # the tool is not running (pressing starts it)
S_STARTING = "starting"    # the tool is being started
S_ERROR = "error"          # the tool runs but reports an error (e.g. PipeWire)
S_ABSENT = "absent"        # the app is not playing right now
S_NOSTREAM = "nostream"    # the app plays, but there is no Go Live stream with sound
S_UNSURE = "unsure"        # there is a stream, but the tool can't tell which Discord stream
S_OFF = "off"              # the app plays, not going to the stream
S_ON = "on"                # the app plays and IS on stream
S_SYS_ON = "sys_on"        # "Whole system" active (enforcement off)
S_SYS_OFF = "sys_off"      # only selected apps

# Badge texts are English msgids, translated by tr() when a Visual is built.
BADGE = {
    S_UNSET: "",
    S_DOWN: "start",
    S_STARTING: "starting…",
    S_ERROR: "error",
    S_ABSENT: "not playing",
    S_NOSTREAM: "no stream",
    S_UNSURE: "unsure",
    S_OFF: "",
    S_ON: "on stream",
    S_SYS_ON: "on",
    S_SYS_OFF: "only selected",
}

SYSTEM_LABEL = "Whole system"
UNSET_LABEL = "pick an app"

# Environment variables NOT passed to the launched tool (they could swap the
# Python interpreter or libraries, e.g. when OpenDeck runs from an AppImage).
CHILD_ENV_DROP = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH",
                  "PYTHONUSERBASE", "LD_LIBRARY_PATH", "LD_PRELOAD")


# --------------------------------------------------------------------------
# Language (mirrors warpipe/i18n.py; the plugin is installed separately)
# --------------------------------------------------------------------------

LANGUAGES = ("en", "pl")
DEFAULT_LANGUAGE = "en"
LANG_ENV_VAR = "WARPIPE_LANG"
LOCALE_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")

PL = {
    # key badges and labels
    "start": "uruchom",
    "starting…": "uruchamiam…",
    "error": "błąd",
    "not playing": "brak",
    "no stream": "brak streamu",
    "unsure": "niepewny",
    "on stream": "na streamie",
    "on": "włączony",
    "only selected": "tylko wybrane",
    "Whole system": "Cały system",
    "pick an app": "wybierz aplikację",
    # key presses
    "This key has no app selected — pick one in the key settings.":
        "Przycisk nie ma wybranej aplikacji — wybierz ją w ustawieniach przycisku.",
    "App {app} is not playing right now — nothing to toggle.":
        "Aplikacja {app} teraz nie gra — nie ma czego przełączać.",
    # finding the tool
    "The program path must be absolute (start with / or ~/): {path}":
        "Ścieżka do programu musi być pełna (zaczynać się od / albo ~/): {path}",
    "Program not found (or not executable): {path}":
        "Nie ma programu (albo nie da się go uruchomić): {path}",
    "Can't find the warpipe program (not in PATH or ~/.local/bin). "
    "Install it or set its path in the key settings (Advanced).":
        "Nie znalazłem programu warpipe (ani w PATH, ani w ~/.local/bin). "
        "Zainstaluj go albo podaj ścieżkę w ustawieniach przycisku (Zaawansowane).",
    # plugin process (OpenDeck log)
    "unknown error": "nieznany błąd",
    "Warpipe reported an error: {error}": "Warpipe zgłosił błąd: {error}",
    "not connected to Warpipe": "brak połączenia z programem Warpipe",
    "failed to start {program}: {error}": "nie udało się uruchomić {program}: {error}",
    "starting Warpipe: {program} --tray": "uruchamiam Warpipe: {program} --tray",
}

_language = DEFAULT_LANGUAGE


def normalize_language(value) -> str | None:
    """"en" / "pl", or None for "auto" (and missing/empty). Other languages → "en"."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or v == "auto":
        return None
    return "pl" if v.startswith("pl") else "en"


def config_path(env=None) -> str:
    """The tool's config file (same as warpipe.state.config_dir() / "config.json")."""
    env = os.environ if env is None else env
    d = env.get("WARPIPE_CONFIG_DIR")
    if not d:
        base = env.get("XDG_CONFIG_HOME") or os.path.join(
            env.get("HOME") or os.path.expanduser("~"), ".config")
        d = os.path.join(base, "warpipe")
    return os.path.join(d, "config.json")


def _config_language(env) -> str | None:
    try:
        with open(config_path(env), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data.get("language") if isinstance(data, dict) else None


def resolve_language(env=None) -> str:
    """WARPIPE_LANG, then the tool's config key "language", then the locale."""
    env = os.environ if env is None else env
    lang = normalize_language(env.get(LANG_ENV_VAR)) or normalize_language(_config_language(env))
    if lang:
        return lang
    for var in LOCALE_VARS:
        v = env.get(var)
        if v:
            return "pl" if v.strip().lower().startswith("pl") else "en"
    return DEFAULT_LANGUAGE


def set_language(lang) -> str:
    """Set "en" or "pl" (anything else / "auto" → English). Returns the language set."""
    global _language
    _language = normalize_language(lang) or DEFAULT_LANGUAGE
    return _language


def language() -> str:
    return _language


def tr(msgid: str) -> str:
    if _language == "pl" and msgid:
        return PL.get(msgid, msgid)
    return msgid


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def runtime_dir(env=None) -> str:
    """Same as warpipe.state.runtime_dir()."""
    env = os.environ if env is None else env
    d = env.get("WARPIPE_RUNTIME_DIR")
    if d:
        return d
    base = env.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return os.path.join(base, "warpipe")
    return os.path.join(tempfile.gettempdir(), f"warpipe-{os.getuid()}")


def control_socket_path(env=None) -> str:
    return os.path.join(runtime_dir(env), "control.sock")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def clean_text(value, limit: int = MAX_SETTING_LEN) -> str:
    """Text from settings: str only, no control characters, trimmed."""
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
    """Shorter caption: "Warframe.x64.exe" -> "Warframe"."""
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
# Tool state
# --------------------------------------------------------------------------

def normalize_status(raw) -> dict | None:
    """Validate the shape of a status from the socket; bad shape -> None ("no status")."""
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
    """The app is selected "exclusively" (enforcement on and the app selected)."""
    a = find_app(status, key)
    return bool(status and status.get("enforce") and a and a.get("selected"))


# --------------------------------------------------------------------------
# Key appearance
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Visual:
    kind: str           # "app" | "system"
    state: str          # S_*
    label: str          # caption (app name)
    badge: str          # small text at the top (already translated)
    ring: bool = False  # green ring: selected, but not on stream right now

    @property
    def tag(self) -> str:
        return f"{self.kind}:{self.state}"


def _v(kind: str, state: str, label: str, ring: bool = False) -> Visual:
    return Visual(kind, state, label, tr(BADGE[state]), ring)


def app_visual(settings: dict, status: dict | None, tool_running: bool, starting: bool = False) -> Visual:
    key = settings.get("app", "")
    if not key:
        return _v("app", S_UNSET, tr(UNSET_LABEL))
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
        return _v("system", S_STARTING if starting else S_DOWN, tr(SYSTEM_LABEL))
    if status.get("enforce"):
        return _v("system", S_SYS_OFF, tr(SYSTEM_LABEL))
    return _v("system", S_SYS_ON, tr(SYSTEM_LABEL))


def visual_for(action: str, settings: dict, status: dict | None, tool_running: bool,
               starting: bool = False) -> Visual:
    if action == ACTION_SYSTEM:
        return system_visual(status, tool_running, starting)
    return app_visual(settings, status, tool_running, starting)


# --------------------------------------------------------------------------
# Key press
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
        return Press("alert", reason=tr("This key has no app selected — pick one in the key settings."))
    if not tool_running or status is None:
        return Press("none") if starting else Press("launch")
    a = find_app(status, key)
    if (a is None or not a.get("present")) and not is_effective(status, key):
        return Press("alert", reason=tr("App {app} is not playing right now — nothing to toggle.").format(app=key))
    return Press("toggle", app=key)


# --------------------------------------------------------------------------
# Property inspector (app list)
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
        "lang": _language,
    }


# --------------------------------------------------------------------------
# Starting the tool
# --------------------------------------------------------------------------

def _is_exec_file(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(path, os.X_OK)


def resolve_tool(global_settings: dict, env=None, which=shutil.which) -> tuple[list[str] | None, str]:
    """Command that starts the tool's tray icon: (argv, problem description).

    Always an argument list (no shell). Order: path from the plugin settings,
    warpipe from PATH, ~/.local/bin, ~/.local/share/warpipe."""
    env = os.environ if env is None else env
    configured = (global_settings or {}).get("toolPath", "")
    home = env.get("HOME") or os.path.expanduser("~")
    if configured:
        p = configured
        if p.startswith("~/"):
            p = os.path.join(home, p[2:])
        if not os.path.isabs(p):
            return None, tr("The program path must be absolute (start with / or ~/): {path}").format(path=configured)
        if not _is_exec_file(p):
            return None, tr("Program not found (or not executable): {path}").format(path=p)
        return [p, "--tray"], ""
    # Absolute PATH entries only: "." or empty entries would mean the current
    # directory (and OpenDeck starts the plugin in its own directory).
    path = os.pathsep.join(p for p in (env.get("PATH") or "").split(os.pathsep) if os.path.isabs(p))
    found = which(TOOL_NAME, path=path) if path else None
    if found and os.path.isabs(found) and _is_exec_file(found):
        return [found, "--tray"], ""
    for p in (os.path.join(home, ".local", "bin", TOOL_NAME),
              os.path.join(home, ".local", "share", "warpipe", TOOL_NAME)):
        if _is_exec_file(p):
            return [p, "--tray"], ""
    return None, tr("Can't find the warpipe program (not in PATH or ~/.local/bin). "
                    "Install it or set its path in the key settings (Advanced).")


def child_env(env=None) -> dict:
    env = dict(os.environ if env is None else env)
    for k in CHILD_ENV_DROP:
        env.pop(k, None)
    return env
