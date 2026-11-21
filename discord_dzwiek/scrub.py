"""Zrzut diagnostyczny pw-dump bez prywatnych danych.

Usuwamy: media.name i inne opisy treści (tytuły kart, utworów), dane hosta,
użytkownika i procesu (także user-name/host-name obiektu Core i core.name,
który ma postać pipewire-<użytkownik>-<pid>). Zostawiamy strukturę grafu, klasy, nazwy węzłów i portów
oraz to, CZY dana właściwość istniała (np. application.process.id) — to ważne
przy diagnozie, bo Discord pomija strumienie bez PID.
"""

from __future__ import annotations

import copy
from typing import Any

MEDIA_PLACEHOLDER = "<media>"
SCRUBBED = "<scrubbed>"

MEDIA_KEYS = {
    "media.name", "media.title", "media.artist", "media.album", "media.filename",
    "media.copyright", "media.software", "media.comment", "media.genre",
    "media.date", "media.icon_name", "media.icon", "media.language",
    "node.description",  # dla strumieni bywa tytułem
}
SCRUB_KEYS = {
    "application.process.host", "application.process.user",
    "application.process.machine-id", "application.process.session-id",
    "application.process.id", "pipewire.sec.pid", "pipewire.sec.uid",
    "pipewire.sec.gid", "pipewire.sec.label", "window.x11.display",
    "object.path", "device.serial", "api.alsa.card.longname",
    "api.bluez5.address", "device.string", "api.v4l2.cap.bus_info",
    "application.icon-name", "application.process.args", "remote.name",
    # Core: info.user-name / info.host-name; props core.name = "pipewire-<user>-<pid>"
    "user-name", "host-name", "core.name", "config.name",
}
# Dla węzłów urządzeń node.description jest przydatne i niegroźne.
_DEVICE_CLASSES_PREFIX = ("Audio/Sink", "Audio/Source", "Video/", "Midi/")


def _scrub_props(props: dict, is_device: bool) -> None:
    for k in list(props):
        if k in MEDIA_KEYS:
            if k == "node.description" and is_device:
                continue
            props[k] = MEDIA_PLACEHOLDER
        elif k in SCRUB_KEYS:
            props[k] = SCRUBBED


def _scrub_any(value: Any) -> Any:
    """Rekurencyjnie: słowniki (klucze jak wyżej) i płaskie listy klucz/wartość (Tag.info)."""
    if isinstance(value, dict):
        for k, v in list(value.items()):
            if k in MEDIA_KEYS and k != "node.description":
                value[k] = MEDIA_PLACEHOLDER
            elif k in SCRUB_KEYS:
                value[k] = SCRUBBED
            else:
                value[k] = _scrub_any(v)
        return value
    if isinstance(value, list):
        out = list(value)
        for i, v in enumerate(out):
            if isinstance(v, str) and i + 1 < len(out) and isinstance(out[i + 1], str):
                if v in MEDIA_KEYS:
                    out[i + 1] = MEDIA_PLACEHOLDER
                elif v in SCRUB_KEYS:
                    out[i + 1] = SCRUBBED
            else:
                out[i] = _scrub_any(v)
        return out
    return value


def scrub_dump(objs: list) -> list:
    out = copy.deepcopy(objs)
    for o in out:
        if not isinstance(o, dict):
            continue
        info = o.get("info")
        if isinstance(info, dict):
            props = info.get("props")
            if isinstance(props, dict):
                mc = str(props.get("media.class") or "")
                _scrub_props(props, mc.startswith(_DEVICE_CLASSES_PREFIX))
            for k, v in list(info.items()):
                if k == "props":
                    continue
                if k in SCRUB_KEYS:          # np. Core: info.user-name / info.host-name
                    info[k] = SCRUBBED
                else:
                    info[k] = _scrub_any(v)
        if "props" in o and isinstance(o["props"], dict):
            _scrub_props(o["props"], False)
        if "metadata" in o:
            o["metadata"] = _scrub_any(o["metadata"])
    return out
