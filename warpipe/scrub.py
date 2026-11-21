"""Diagnostic pw-dump without private data.

Removed: media.name and other content descriptions (tab and track titles), host,
user and process data (including user-name/host-name of the Core object and
core.name, which looks like pipewire-<user>-<pid>). Kept: the graph structure,
classes, node and port names, and WHETHER a property existed (e.g.
application.process.id) — this matters for diagnosis, since Discord skips
streams without a PID.
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
    "node.description",  # for streams it is often a title
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
# For device nodes node.description is useful and harmless.
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
    """Recursively: dicts (keys as above) and flat key/value lists (Tag.info)."""
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
