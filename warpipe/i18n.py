"""Tiny gettext-like translation helper.

English is the source language: every user-facing string in the code is an
English msgid wrapped in ``_()`` (or ``N_()`` for module-level constants that
are translated later, at display time; ``ngettext()`` for plurals). The
Polish translation lives in ``PL`` (msgid -> msgstr) and ``PL_PLURAL``
(plural msgid -> the three Polish plural forms). Messages may contain
``str.format`` placeholders; callers format the *translated* string, so a
translation must keep the same placeholder names.

Language choice (first match wins):
  1. environment variable WARPIPE_LANG: "en" | "pl" | "auto"
  2. config key "language" in ~/.config/warpipe/config.json: "auto" | "en" | "pl"
  3. auto: the first non-empty of LC_ALL, LC_MESSAGES, LANG; a value starting
     with "pl" means Polish, anything else English.

The language is resolved lazily on first use and then cached for the process.
"""

from __future__ import annotations

import json
import os

LANGUAGES = ("en", "pl")
DEFAULT_LANGUAGE = "en"
ENV_VAR = "WARPIPE_LANG"
CONFIG_KEY = "language"
LOCALE_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")

_lang: str | None = None


def normalize(value) -> str | None:
    """"en" / "pl", or None for "auto" (and for missing/empty values).

    Unsupported languages ("de", "C", ...) fall back to English."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or v == "auto":
        return None
    return "pl" if v.startswith("pl") else "en"


def locale_language(env=None) -> str:
    env = os.environ if env is None else env
    for var in LOCALE_VARS:
        v = env.get(var)
        if v:
            return "pl" if v.strip().lower().startswith("pl") else "en"
    return DEFAULT_LANGUAGE


def detect_language(setting=None, env=None) -> str:
    """Language for a config value ("auto"/"en"/"pl"/None) and an environment."""
    env = os.environ if env is None else env
    return normalize(env.get(ENV_VAR)) or normalize(setting) or locale_language(env)


def _config_setting():
    """The "language" key of the config file (read-only; errors mean "auto")."""
    try:
        from .state import config_dir
        with open(config_dir() / "config.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, ImportError):
        return None
    return data.get(CONFIG_KEY) if isinstance(data, dict) else None


def language() -> str:
    global _lang
    if _lang is None:
        _lang = detect_language(_config_setting())
    return _lang


def set_language(lang: str | None) -> None:
    """Force "en" or "pl"; None (or "auto") = resolve again on next use."""
    global _lang
    _lang = normalize(lang)


def gettext(msgid: str) -> str:
    if language() == "pl":
        return PL.get(msgid, msgid)
    return msgid


_ = gettext


def N_(msgid: str) -> str:
    """Mark a string for translation without translating it (yet)."""
    return msgid


def _pl_plural_form(n: int) -> int:
    n = abs(int(n))
    if n == 1:
        return 0
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return 1
    return 2


def ngettext(singular: str, plural: str, n: int) -> str:
    if language() == "pl":
        forms = PL_PLURAL.get(plural)
        if forms:
            return forms[_pl_plural_form(n)]
    return singular if n == 1 else plural


# ---------------------------------------------------------------------------
# Polish catalog
# ---------------------------------------------------------------------------

PL: dict[str, str] = {
    # ---- planner: detection status, kinds, reasons, plan notes
    "Discord Go Live audio: active": "Stream z dźwiękiem: wykryty",
    "Discord Go Live audio: not detected": "Stream z dźwiękiem: nie wykryty",
    "Discord Go Live audio: ambiguous — pick one in the menu": "Stream z dźwiękiem: niepewny — wybierz w menu",
    "Discord Go Live audio: ambiguous — pick one in the tray menu":
        "Stream z dźwiękiem: niepewny — wybierz w menu ikony",
    "Discord Go Live audio: invalid node choice": "Stream z dźwiękiem: zły wybór węzła",
    "Discord Go Live audio: ?": "Stream z dźwiękiem: ?",
    "stream audio": "dźwięk streamu",
    "microphone (skipped)": "mikrofon (pomijam)",
    "unknown": "nie wiadomo",
    "target: app stream": "cel: strumień aplikacji",
    "target: audio output (monitor)": "cel: wyjście audio (monitor)",
    "target: vencord-screen-share": "cel: vencord-screen-share",
    "recognized earlier": "rozpoznany wcześniej",
    "input from an output monitor": "wejście z monitora wyjścia",
    "input from an app stream": "wejście ze strumienia aplikacji",
    "input from vencord-screen-share": "wejście z vencord-screen-share",
    "target: microphone/source": "cel: mikrofon/źródło",
    "input from a microphone/source": "wejście z mikrofonu/źródła",
    "named like the call microphone": "nazwa jak mikrofon rozmowy",
    "named discord_capture/game capture": "nazwa discord_capture/game capture",
    "no links and no target": "brak połączeń i celu",
    ", app: {app}": ", aplikacja: {app}",
    "node with serial {serial} does not exist or is not a recording stream":
        "węzeł o serialu {serial} nie istnieje albo nie jest strumieniem nagrywania",
    "#{id} looks like a microphone — leaving it alone": "#{id} wygląda na mikrofon — nie ruszam go",
    "chosen manually": "wybrany ręcznie",
    "stream recognized by its links": "strumień rozpoznany po połączeniach",
    "the Discord stream reads an output monitor — it could also be a microphone; "
    "confirm it once in the menu (or with --capture ID)":
        "strumień Discorda czyta monitor wyjścia — to może być też mikrofon; "
        "potwierdź raz w menu (albo --capture ID)",
    "The Discord stream reads an output monitor — it could be a microphone. "
    "If it is the stream audio, pick it once under “Which Discord stream?”.":
        "Strumień Discorda czyta monitor wyjścia — to może być mikrofon. "
        "Jeśli to dźwięk streamu, wybierz go raz w „Który strumień Discorda?”.",
    "can't tell which Discord stream carries the stream audio — pick one manually":
        "nie wiem, który strumień Discorda to dźwięk streamu — wybierz ręcznie",
    "no stream audio capture (Go Live without sound, or not streaming)":
        "brak strumienia dźwięku streamu (Go Live bez dźwięku albo wyłączony)",
    "Warning: a microphone/source is connected to the stream audio (#{id}).":
        "Uwaga: do dźwięku streamu (#{id}) podłączony jest mikrofon/źródło.",
    "Warning: the stream audio capture (#{id}) is muted — viewers will hear nothing.":
        "Uwaga: przechwytywanie dźwięku streamu (#{id}) jest wyciszone — widzowie nic nie usłyszą.",
    "{app} (#{id}) has no output ports yet": "{app} (#{id}) nie ma jeszcze portów wyjściowych",
    "capture stream #{id} has no input ports yet": "strumień przechwytujący #{id} nie ma jeszcze portów wejściowych",
    "the snapshot is from a different PipeWire session — not restoring links from it":
        "migawka pochodzi z innego uruchomienia PipeWire — nie odtwarzam z niej połączeń",

    # ---- enforcer: routing line, events, results
    "PipeWire error": "Błąd PipeWire",
    "To Discord: all system audio": "Do Discorda: cały dźwięk systemu",
    "To Discord: nothing (stream audio muted)": "Do Discorda: nic (dźwięk streamu wyciszony)",
    "No app selected — the stream has no audio until you tick one":
        "Nie zaznaczono żadnej aplikacji — stream nie ma dźwięku, dopóki jakiejś nie zaznaczysz",
    "To Discord only: {apps}": "Do Discorda tylko: {apps}",
    "Can't save settings: {error}": "Nie mogę zapisać ustawień: {error}",
    "Go Live audio detected.": "Wykryto stream z dźwiękiem.",
    "Something keeps changing the links (Discord/WirePlumber?) — paused for a moment.":
        "Coś ciągle zmienia połączenia (Discord/WirePlumber?) — chwilowo wstrzymane.",
    "Something keeps changing the links to Discord — pausing enforcement for a moment.":
        "Coś ciągle zmienia połączenia do Discorda — wstrzymuję pilnowanie na chwilę.",
    "Can't save the link snapshot ({error}) — not changing anything.":
        "Nie mogę zapisać migawki połączeń ({error}) — nic nie zmieniam.",
    "{error} — not removing the remaining links.": "{error} — nie usuwam pozostałych połączeń.",
    "linked {created}, unlinked {deleted}": "połączono {created}, rozłączono {deleted}",
    "restored {created}, removed {deleted}": "przywrócono {created}, usunięto {deleted}",
    "Enabled. Takes effect when you start a Go Live stream with sound.":
        "Włączone. Zadziała, gdy zaczniesz stream z dźwiękiem.",
    "All system audio goes to Discord.": "Do Discorda idzie cały dźwięk systemu.",
    "Restore failed: {error}": "Nie udało się przywrócić: {error}",
    "No change: {routing}": "Bez zmian: {routing}",
    "Added {app}.": "Dodano {app}.",
    "Removed {app}.": "Odznaczono {app}.",
    "Removed {app}. Nothing is selected, so all system audio goes to Discord.":
        "Odznaczono {app}. Nic nie jest zaznaczone, więc do Discorda idzie cały dźwięk systemu.",
    "App “{app}” is not playing right now — not changing anything.":
        "Aplikacja „{app}” teraz nie gra — nic nie zmieniam.",
    "Invalid app name (it must be non-empty UTF-8 text).":
        "Niepoprawna nazwa aplikacji (musi to być niepusty tekst UTF-8).",

    # ---- control channel
    "field {field} must be a string": "pole {field} musi być tekstem",
    "field {field} is empty": "puste pole {field}",
    "field {field} is too long (max {limit} characters)": "pole {field} jest za długie (maks. {limit} znaków)",
    "field {field} contains control characters or invalid text":
        "pole {field} zawiera znaki sterujące albo niepoprawny tekst",
    "field {field} must be true or false": "pole {field} musi mieć wartość true albo false",
    'field on must be true/false or "on"/"off"': 'pole on musi być true/false albo "on"/"off"',
    "line too long (max {limit} bytes)": "za długa linia (maks. {limit} bajtów)",
    "invalid encoding (UTF-8 expected)": "niepoprawne kodowanie (oczekiwane UTF-8)",
    "invalid JSON": "niepoprawny JSON",
    "JSON object expected": "oczekiwany obiekt JSON",
    "missing field cmd": "brak pola cmd",
    "unknown command: {cmd} (known: {known})": "nieznane polecenie: {cmd} (znane: {known})",
    "unknown command: {cmd}": "nieznane polecenie: {cmd}",
    "{path} is not a directory": "{path} nie jest katalogiem",
    "directory {path} belongs to another user": "katalog {path} należy do innego użytkownika",
    "{path} exists and is not a socket — leaving it alone": "{path} istnieje i nie jest gniazdem — nie ruszam go",
    "socket {path} belongs to another user": "gniazdo {path} należy do innego użytkownika",
    "can't check socket {path}: {error}": "nie mogę sprawdzić gniazda {path}: {error}",
    "something is already listening on {path}": "na {path} już ktoś nasłuchuje",
    "can't create socket {path}: {error}": "nie mogę utworzyć gniazda {path}: {error}",
    "too many connections": "za dużo połączeń",
    "line too long (max {limit} bytes) — disconnecting": "za długa linia (maks. {limit} bajtów) — rozłączam",
    "internal error (see the log)": "błąd wewnętrzny (szczegóły w logu)",
    "Warpipe is not running (neither the tray icon nor --watch).":
        "Warpipe nie jest uruchomiony (ani ikona, ani --watch).",
    "Can't connect to {path}: {error}": "Nie mogę połączyć się z {path}: {error}",
    "The running instance did not answer in time.": "Działająca instancja nie odpowiedziała na czas.",
    "The running instance closed the connection without answering.":
        "Działająca instancja zamknęła połączenie bez odpowiedzi.",
    "Reply too long.": "Za długa odpowiedź.",
    "Invalid reply from the running instance.": "Niepoprawna odpowiedź działającej instancji.",
    "The request contains text that is not valid UTF-8.": "Polecenie zawiera tekst, który nie jest poprawnym UTF-8.",

    # ---- PipeWire tools
    "Program {program} not found — are the PipeWire tools (pipewire-utils) installed?":
        "Nie znaleziono programu {program} — czy PipeWire (pipewire-utils) jest zainstalowany?",
    "{program} did not answer within {seconds:.0f} s — is PipeWire running?":
        "{program} nie odpowiedział w {seconds:.0f} s — czy PipeWire działa?",
    "Can't run {program}: {error}": "Nie mogę uruchomić {program}: {error}",
    "Can't read the PipeWire state (pw-dump, exit code {code}): {details}":
        "Nie mogę odczytać stanu PipeWire (pw-dump, kod {code}): {details}",
    "pw-dump returned invalid data — is PipeWire running?": "pw-dump zwrócił niepoprawne dane — czy PipeWire działa?",
    "pw-dump returned data in an unexpected format.": "pw-dump zwrócił nieoczekiwany format danych.",
    "pw-link failed to link ports {src} -> {dst}: {details}": "pw-link nie połączył portów {src} -> {dst}: {details}",
    "pw-link failed to unlink ports {src} -> {dst}: {details}": "pw-link nie rozłączył portów {src} -> {dst}: {details}",
    "Can't run pw-dump -m: {error}": "Nie mogę uruchomić pw-dump -m: {error}",

    # ---- config / snapshot
    "{path}: invalid format (a JSON object was expected)": "{path}: zły format (oczekiwany obiekt JSON)",
    "Can't read {path}: {error}": "Nie mogę odczytać {path}: {error}",
    "Can't read the snapshot {path}: {error}": "Nie mogę odczytać migawki {path}: {error}",

    # ---- command line
    "{app}: only the apps you pick are heard in your Discord Go Live stream.":
        "{app}: do dźwięku streamu w Discordzie (Go Live) trafiają tylko wybrane aplikacje.",
    "Without arguments (in a graphical session) it starts the tray icon.":
        "Bez argumentów (w sesji graficznej) uruchamia ikonę w zasobniku.",
    "show Discord capture streams and apps playing audio": "pokaż strumienie Discorda i aplikacje z dźwiękiem",
    "(with --list or --status) output JSON": "(z --list albo --status) wynik w formacie JSON",
    "state of the RUNNING instance (tray icon or --watch): mode, stream, apps":
        "stan DZIAŁAJĄCEJ instancji (ikona albo --watch): tryb, stream, aplikacje",
    "in the running instance: add/remove an app's audio to/from the stream (like the OpenDeck key)":
        "w działającej instancji: dodaj/zabierz dźwięk aplikacji ze streamu (jak przycisk w OpenDeck)",
    "with --toggle: select the app even if it is not playing right now":
        "z --toggle: zaznacz aplikację, nawet jeśli teraz nie gra",
    "in the running instance: “Whole system” (restore Discord's normal behaviour)":
        "w działającej instancji: „Cały system” (przywróć zwykłe zachowanie Discorda)",
    "one-shot: only these apps go to the stream audio (names as in --list)":
        "jednorazowo: do dźwięku streamu tylko te aplikacje (nazwy jak w --list)",
    "id of Discord's capture node (from --list) when detection is ambiguous; may be given more than once":
        "id węzła przechwytującego Discorda (z --list), gdy wykrywanie jest niepewne; można podać kilka razy",
    "restore the links from before the changes (whole system)": "przywróć połączenia sprzed zmian (cały system)",
    "enforce without the tray icon (apps from --apply or from the config); Ctrl+C restores":
        "pilnuj bez ikony (aplikacje z --apply albo z konfiguracji); Ctrl+C przywraca",
    "only show what would change": "tylko pokaż, co zostałoby zmienione",
    "FILE": "PLIK",
    "write a diagnostic pw-dump without private data ('-' = stdout)":
        "zapisz diagnostyczny pw-dump bez prywatnych danych ('-' = stdout)",
    "tray icon (the default without arguments)": "ikona w zasobniku (domyślnie, gdy brak argumentów)",
    "more log messages": "więcej komunikatów",
    "No node with id {id} (check --list).": "Nie ma węzła o id {id} (sprawdź --list).",
    "Discord capture streams:": "Strumienie nagrywania Discorda:",
    "(none)": "(brak)",
    "serial {serial}; reasons: {reasons}": "serial {serial}; powody: {reasons}",
    "only via an output monitor (it could be a microphone) — confirm with: --capture {id}":
        "tylko po monitorze wyjścia (może to być mikrofon) — potwierdź: --capture {id}",
    "ours": "nasze",
    "Apps playing audio (name for --apply):": "Aplikacje z dźwiękiem (nazwa do --apply):",
    "no PID — Discord does not capture it by itself": "bez PID — Discord sam tego nie łapie",
    "goes to Discord": "idzie do Discorda",
    "Config: {path}": "Konfiguracja: {path}",
    "selected: {apps}; enforce at start: {enforce}": "wybrane: {apps}; pilnowanie przy starcie: {enforce}",
    "(nothing)": "(nic)",
    "yes": "tak",
    "no": "nie",
    "Nothing to change.": "Nic do zmiany.",
    "link": "połącz",
    "unlink": "rozłącz",
    "(note: {note})": "(uwaga: {note})",
    "“{name}”": "„{name}”",
    "None of the given apps is playing right now ({names}) — not changing anything.":
        "Nie widzę teraz żadnej z podanych aplikacji ({names}) — nic nie zmieniam.",
    "Apps playing right now (name for --apply): {apps}": "Aplikacje, które teraz grają (nazwa do --apply): {apps}",
    "No app is playing right now.": "Żadna aplikacja teraz nie gra.",
    "Warning: app “{name}” is not playing right now — it will NOT go to Discord (check the name with --list).":
        "Uwaga: nie widzę teraz aplikacji „{name}” — ona NIE pójdzie do Discorda (sprawdź nazwę w --list).",
    "Discord stream: {streams}": "Strumień Discorda: {streams}",
    "Error: {error}": "Błąd: {error}",
    "Done: {action}. To undo: warpipe --restore": "Gotowe: {action}. Cofnięcie: warpipe --restore",
    "no changes": "bez zmian",
    "Candidates (pass --capture ID):": "Kandydaci (podaj --capture ID):",
    "Restore error: {error}": "Błąd przywracania: {error}",
    "Restored ({details}).": "Przywrócono ({details}).",
    "Warning: no app selected — nothing will go to Discord.":
        "Uwaga: nie wybrano żadnej aplikacji — do Discorda nie pójdzie nic.",
    "Restore error on exit: {error}": "Błąd przywracania przy wyjściu: {error}",
    "Apps:": "Aplikacje:",
    "(no app is playing right now)": "(żadna aplikacja teraz nie gra)",
    "not playing now": "teraz nie gra",
    "on stream": "na streamie",
    "Apps playing right now: {apps}": "Aplikacje, które teraz grają: {apps}",
    "To select it anyway (e.g. before the game starts): warpipe --toggle {app} --force":
        "Żeby mimo to ją zaznaczyć (np. przed uruchomieniem gry): warpipe --toggle {app} --force",
    "{error} Start it with: warpipe (tray icon) or warpipe --watch.":
        "{error} Uruchom go: warpipe (ikona) albo warpipe --watch.",
    "unknown error": "nieznany błąd",
    "Saved {path} (without stream titles or personal data).":
        "Zapisano {path} (bez tytułów strumieni i danych osobistych).",
    "choose one of: --list, --apply, --restore, --watch, --dump, --tray, --status, "
    "--toggle, --system (exception: --watch --apply)":
        "wybierz jedno: --list, --apply, --restore, --watch, --dump, --tray, --status, "
        "--toggle, --system (wyjątek: --watch --apply)",
    "--json only works with --list or --status": "--json działa tylko z --list albo --status",
    "--dry-run works with --apply or --restore": "--dry-run działa z --apply albo --restore",
    "--toggle needs an app name (as shown by --list)": "--toggle wymaga nazwy aplikacji (jak w --list)",
    "--toggle: the app name is not valid UTF-8 text": "--toggle: nazwa aplikacji nie jest poprawnym tekstem UTF-8",
    "--force only works with --toggle": "--force działa tylko z --toggle",
    "Warning: {error} — using default settings.": "Uwaga: {error} — używam ustawień domyślnych.",
    "tray icon or --watch": "ikona albo --watch",
    "Another instance is already running ({who}). Quit it or use its menu.":
        "Działa już inna instancja ({who}). Zamknij ją albo użyj jej menu.",

    # ---- tray icon
    "Only selected apps": "Tylko zaznaczone do Discorda",
    "Whole system (restore)": "Cały system (przywróć)",
    "Apps playing audio:": "Aplikacje z dźwiękiem:",
    "{app} (not playing now)": "{app} (teraz nie gra)",
    "Which Discord stream?": "Który strumień Discorda?",
    "Automatic": "Automatycznie",
    "check that it isn't a microphone": "sprawdź, czy to nie mikrofon",
    "Quit": "Zakończ",
    "Warpipe is already running — its icon is in the system tray (next to the clock).":
        "Warpipe już działa — jego ikona jest w zasobniku (obok zegara).",
}

# plural msgid -> (1, 2-4, 5+) forms
PL_PLURAL: dict[str, tuple[str, str, str]] = {
    "{n} streams": ("{n} strumień", "{n} strumienie", "{n} strumieni"),
    "{n} discord_capture streams": ("{n} strumień discord_capture", "{n} strumienie discord_capture",
                                    "{n} strumieni discord_capture"),
}
