"""Syntetyczne grafy podobne do typowego pulpitu KDE (EasyEffects, Discord flatpak)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from helpers import GraphBuilder

from warpipe.planner import DetectConfig

ALSA_SINK = "alsa_output.usb-Generic_USB_Headset-00.analog-stereo"
HDMI_SINK = "alsa_output.pci-0000_09_00.1.hdmi-stereo"
EE_SINK = "easyeffects_sink"

# Strumień „Discord” czytający monitor wyjścia to słaby dowód (tak samo wygląda
# mikrofon ustawiony na monitor) — wymaga jednorazowego potwierdzenia przez
# użytkownika. W testach trybu „monitor” potwierdzenie jest już zapisane.
MONITOR_CONFIRMED = tuple(("Discord", "Discord", s) for s in (ALSA_SINK, HDMI_SINK, EE_SINK))
CFG_MON = DetectConfig(confirmed=MONITOR_CONFIRMED)


def write_confirmed_config(path, **extra) -> None:
    """config.json z potwierdzonymi strumieniami „z monitora” (jak po wyborze w menu)."""
    data = {"version": 1,
            "confirmed_captures": [{"program": p, "node": n, "sink": s} for p, n, s in MONITOR_CONFIRMED]}
    data.update(extra)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def base_system(b: GraphBuilder) -> SimpleNamespace:
    """Urządzenia + EasyEffects + rozmowa głosowa Discorda (playStream/recStream)."""
    ns = SimpleNamespace()
    ns.alsa = b.sink(ALSA_SINK)
    ns.hdmi = b.sink(HDMI_SINK)
    ns.mic = b.source("alsa_input.usb-Generic_USB_Headset-00.mono-fallback", channels=("MONO",))
    ns.ee_sink = b.sink(EE_SINK)
    ns.ee_src = b.source("easyeffects_source", media_class="Audio/Source/Virtual")
    dcl = b.client("WEBRTC VoiceEngine", "Discord", **{"pipewire.access": "flatpak"})
    common = {"application.name": "WEBRTC VoiceEngine", "application.process.binary": "Discord",
              "application.process.id": 777, "client.api": "pipewire-pulse"}
    ns.play = b.node("Stream/Output/Audio", "WEBRTC VoiceEngine", client=dcl,
                     props=dict(common, **{"media.name": "playStream", "target.object": ns.alsa.name}),
                     outs=("FL", "FR"))
    ns.rec = b.node("Stream/Input/Audio", "WEBRTC VoiceEngine", client=dcl,
                    props=dict(common, **{"media.name": "recStream", "target.object": ns.mic.name}),
                    ins=("FL", "FR"), monitor_outs=("FL", "FR"))
    b.connect(ns.play, ns.ee_sink, dst_prefix="playback")
    b.connect(ns.ee_src, ns.rec)
    return ns


def with_apps(b: GraphBuilder, ns: SimpleNamespace) -> SimpleNamespace:
    ns.warframe = b.app("Warframe.x64.exe", "wine64-preloader", node_name="Warframe.x64.exe",
                        media_name="audio stream")
    ns.firefox = b.app("Firefox", "firefox", media_name="SEKRETNY tytuł karty YouTube")
    # Minecraft/OpenAL: natywny PipeWire, węzeł "java" bez application.name i bez PID.
    ns.java = b.app(None, None, node_name="java", pid=False, client_app_name="java",
                    api=None, media_name="Playback Stream")
    for a in (ns.warframe, ns.firefox, ns.java):
        b.connect(a, ns.ee_sink, dst_prefix="playback")
    return ns


def per_app(b: GraphBuilder | None = None) -> tuple[GraphBuilder, SimpleNamespace]:
    """(a) Oficjalny Discord: discord_capture na każdą aplikację z PID, połączenia od WirePlumbera."""
    b = b or GraphBuilder()
    ns = with_apps(b, base_system(b))
    ns.cap_wf = b.capture(target=ns.warframe.serial)
    ns.cap_ff = b.capture(target=ns.firefox.serial)
    b.connect(ns.warframe, ns.cap_wf)
    b.connect(ns.firefox, ns.cap_ff)
    return b, ns


def monitor_mode(sink_attr: str = "ee_sink") -> tuple[GraphBuilder, SimpleNamespace]:
    """(b)/(c) Jeden strumień nagrywania czytający monitor wyjścia."""
    b = GraphBuilder()
    ns = with_apps(b, base_system(b))
    sink = getattr(ns, sink_attr)
    ns.cap = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream",
                       target=sink.name, capture_sink=True)
    b.connect(sink, ns.cap, src_prefix="monitor")
    return b, ns
