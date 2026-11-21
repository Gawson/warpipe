"""PipeWire graph model built from the list of pw-dump objects.

Pure code (no external calls), so it can be tested on saved snapshots. Objects
are identified by object.serial where persistence matters (a serial is never
reused) and by id where pw-link has to be called (pw-link takes port ids).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

T_NODE = "PipeWire:Interface:Node"
T_PORT = "PipeWire:Interface:Port"
T_LINK = "PipeWire:Interface:Link"
T_CLIENT = "PipeWire:Interface:Client"
T_CORE = "PipeWire:Interface:Core"

# Property used to mark links created by this tool (pw-link -p).
# "Whole system" removes such links.
MANAGED_KEY = "warpipe.managed"
# An ORIGINAL link recreated by us (from the snapshot). "Whole system" does not
# remove it — it is the pre-change state; the marker only tells us that we
# created it (post-check when a port id was reused).
RESTORED_KEY = "warpipe.restored"

# App identity fields filled in from the client's properties when the node
# lacks them (e.g. Minecraft/OpenAL: a "java" node without application.name).
CLIENT_FALLBACK_KEYS = (
    "application.name",
    "application.process.binary",
    "application.id",
    "pipewire.access.portal.app_id",
    "client.api",
)

KNOWN_CHANNELS = {
    "MONO", "FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR", "RC", "FLC", "FRC",
    "TC", "TFL", "TFC", "TFR", "TRL", "TRC", "TRR", "RLC", "RRC", "FLW", "FRW",
    "LFE2", "FLH", "FCH", "FRH", "TFLC", "TFRC", "TSL", "TSR", "LLFE", "RLFE",
    "BC", "BLC", "BRC",
}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            try:
                return int(s)
            except ValueError:
                return None
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def _nonempty_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


@dataclass
class Port:
    id: int
    serial: int
    node_id: int
    direction: str  # "in" albo "out"
    name: str
    channel: str | None
    monitor: bool
    port_index: int
    props: dict = field(repr=False, default_factory=dict)


@dataclass
class Node:
    id: int
    serial: int
    props: dict = field(repr=False)          # node properties plus client fallback fields
    own_props: dict = field(repr=False)      # the node's own properties only
    client_props: dict = field(repr=False)
    in_ports: list[Port] = field(default_factory=list)
    out_ports: list[Port] = field(default_factory=list)
    params: dict = field(repr=False, default_factory=dict)

    @property
    def media_class(self) -> str:
        return str(self.own_props.get("media.class") or "")

    @property
    def name(self) -> str:
        return str(self.own_props.get("node.name") or "")

    @property
    def app_name(self) -> str | None:
        return _nonempty_str(self.props.get("application.name"))

    @property
    def binary(self) -> str | None:
        return _nonempty_str(self.props.get("application.process.binary"))

    @property
    def media_name(self) -> str | None:
        """Stream title (e.g. a browser tab). Only for UI hints —
        never write it to logs or the config."""
        return _nonempty_str(self.own_props.get("media.name"))

    @property
    def has_pid(self) -> bool:
        """Does the node ITSELF have application.process.id? Discord reads this field
        from pipewire-pulse's sink-input list (node properties, not the client's);
        without it the stream is skipped (e.g. Minecraft/OpenAL on native PipeWire)."""
        return self.own_props.get("application.process.id") not in (None, "")

    @property
    def target_object(self) -> Any:
        v = self.own_props.get("target.object")
        if v is None:
            v = self.own_props.get("node.target")
        return v

    @property
    def capture_sink(self) -> bool:
        return _as_bool(self.own_props.get("stream.capture.sink"))

    @property
    def out_ports_audio(self) -> list[Port]:
        """Outputs without monitor ports (those are just a copy of the input)."""
        return [p for p in self.out_ports if not p.monitor]

    def mute_state(self) -> bool:
        """True if the node is muted (Props.mute / softMute / all-zero softVolumes)."""
        props_list = self.params.get("Props") if isinstance(self.params, dict) else None
        if not isinstance(props_list, list):
            return False
        for entry in props_list:
            if not isinstance(entry, dict):
                continue
            if entry.get("mute") is True or entry.get("softMute") is True:
                return True
            sv = entry.get("softVolumes")
            if isinstance(sv, list) and sv and all(
                isinstance(x, (int, float)) and x == 0 for x in sv
            ):
                return True
        return False

    def label(self) -> str:
        """Label safe for logs (no media.name)."""
        bits = [self.name or "?"]
        if self.binary:
            bits.append(f"[{self.binary}]")
        return f"#{self.id} " + " ".join(bits)


@dataclass
class Link:
    id: int
    serial: int
    out_node: int
    out_port: int
    in_node: int
    in_port: int
    state: str | None
    props: dict = field(repr=False, default_factory=dict)

    @property
    def managed(self) -> bool:
        return MANAGED_KEY in self.props

    @property
    def ours(self) -> bool:
        """Created by this tool (our extra link or a recreated original)."""
        return MANAGED_KEY in self.props or RESTORED_KEY in self.props


def _instance_id(info: dict) -> str | None:
    """Identity of the current PipeWire daemon session (Core id 0).

    object.serial is a daemon counter — it starts over after a PipeWire restart,
    so a serial from the snapshot may point at a completely different port. The
    cookie is random on every daemon start; the daemon PID is added to be sure.
    """
    cookie = _as_int(info.get("cookie"))
    if cookie is None:
        return None
    props = info.get("props") if isinstance(info.get("props"), dict) else {}
    pid = _as_int(props.get("application.process.id"))
    return f"{cookie}:{pid if pid is not None else '?'}"


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[int, Node] = {}
        self.ports: dict[int, Port] = {}
        self.links: dict[int, Link] = {}
        self.clients: dict[int, dict] = {}
        self.node_by_serial: dict[int, Node] = {}
        self.port_by_serial: dict[int, Port] = {}
        # None = zrzut bez obiektu Core (np. syntetyczne testy albo stary PipeWire)
        self.instance_id: str | None = None

    # ------------------------------------------------------------------ parse
    @classmethod
    def from_dump(cls, objs: Iterable[dict]) -> "Graph":
        g = cls()
        objs = [o for o in objs if isinstance(o, dict)]
        raw_nodes = []
        raw_ports = []
        for o in objs:
            info = o.get("info")
            if not isinstance(info, dict):
                continue
            t = o.get("type")
            oid = _as_int(o.get("id"))
            if oid is None:
                continue
            if t == T_CORE:
                if oid == 0 and g.instance_id is None:
                    g.instance_id = _instance_id(info)
            elif t == T_CLIENT:
                g.clients[oid] = dict(info.get("props") or {})
            elif t == T_NODE:
                raw_nodes.append((oid, info))
            elif t == T_PORT:
                raw_ports.append((oid, info))
            elif t == T_LINK:
                props = dict(info.get("props") or {})
                try:
                    link = Link(
                        id=oid,
                        serial=_as_int(props.get("object.serial")) or oid,
                        out_node=int(info["output-node-id"]),
                        out_port=int(info["output-port-id"]),
                        in_node=int(info["input-node-id"]),
                        in_port=int(info["input-port-id"]),
                        state=info.get("state"),
                        props=props,
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                g.links[oid] = link

        for oid, info in raw_nodes:
            own = dict(info.get("props") or {})
            cid = _as_int(own.get("client.id"))
            cprops = g.clients.get(cid, {}) if cid is not None else {}
            merged = dict(own)
            for k in CLIENT_FALLBACK_KEYS:
                if _nonempty_str(merged.get(k)) is None and _nonempty_str(cprops.get(k)) is not None:
                    merged[k] = cprops[k]
            node = Node(
                id=oid,
                serial=_as_int(own.get("object.serial")) or oid,
                props=merged,
                own_props=own,
                client_props=cprops,
                params=info.get("params") if isinstance(info.get("params"), dict) else {},
            )
            g.nodes[oid] = node
            g.node_by_serial[node.serial] = node

        for oid, info in raw_ports:
            props = dict(info.get("props") or {})
            node_id = _as_int(props.get("node.id"))
            if node_id is None:
                continue
            direction = info.get("direction") or props.get("port.direction")
            if direction in ("input", "in"):
                direction = "in"
            elif direction in ("output", "out"):
                direction = "out"
            else:
                continue
            name = str(props.get("port.name") or "")
            port = Port(
                id=oid,
                serial=_as_int(props.get("object.serial")) or oid,
                node_id=node_id,
                direction=direction,
                name=name,
                channel=_port_channel(props, name),
                monitor=_as_bool(props.get("port.monitor")) or name.startswith("monitor_"),
                port_index=_as_int(props.get("port.id")) if _as_int(props.get("port.id")) is not None else oid,
                props=props,
            )
            g.ports[oid] = port
            g.port_by_serial[port.serial] = port
            node = g.nodes.get(node_id)
            if node is not None:
                (node.in_ports if direction == "in" else node.out_ports).append(port)

        for node in g.nodes.values():
            node.in_ports.sort(key=lambda p: (p.port_index, p.id))
            node.out_ports.sort(key=lambda p: (p.port_index, p.id))
        return g

    # ---------------------------------------------------------------- queries
    def links_into(self, node_id: int) -> list[Link]:
        return sorted((l for l in self.links.values() if l.in_node == node_id), key=lambda l: l.id)

    def links_from(self, node_id: int) -> list[Link]:
        return sorted((l for l in self.links.values() if l.out_node == node_id), key=lambda l: l.id)

    def link_pairs(self) -> dict[tuple[int, int], Link]:
        return {(l.out_port, l.in_port): l for l in self.links.values()}

    def resolve_target(self, value: Any) -> Node | None:
        """Resolve target.object: number = object.serial (or id), text = node.name/object.path."""
        if value is None:
            return None
        num = _as_int(value)
        if num is not None:
            if num < 0:
                return None
            return self.node_by_serial.get(num) or self.nodes.get(num)
        s = str(value)
        for n in sorted(self.nodes.values(), key=lambda n: n.id):
            if n.name == s or n.own_props.get("object.path") == s:
                return n
        return None

    def node(self, node_id: int) -> Node | None:
        return self.nodes.get(node_id)


def _port_channel(props: dict, name: str) -> str | None:
    ch = _nonempty_str(props.get("audio.channel"))
    if ch:
        ch = ch.upper()
        return ch if ch in KNOWN_CHANNELS else None
    if "_" in name:
        suffix = name.rsplit("_", 1)[1].upper()
        if suffix in KNOWN_CHANNELS:
            return suffix
    return None
