"""Model grafu PipeWire zbudowany z listy obiektów pw-dump.

Czysty kod (bez wywołań zewnętrznych), żeby dało się go testować na zapisanych
zrzutach. Obiekty identyfikujemy po object.serial tam, gdzie liczy się
trwałość (serial nigdy nie jest używany ponownie), a po id tam, gdzie trzeba
wywołać pw-link (pw-link przyjmuje id portów).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

T_NODE = "PipeWire:Interface:Node"
T_PORT = "PipeWire:Interface:Port"
T_LINK = "PipeWire:Interface:Link"
T_CLIENT = "PipeWire:Interface:Client"
T_CORE = "PipeWire:Interface:Core"

# Klucz, którym znakujemy połączenia utworzone przez to narzędzie (pw-link -p).
# Takie połączenia „Cały system” usuwa.
MANAGED_KEY = "warpipe.managed"
# Odtworzone przez nas ORYGINALNE połączenie (z migawki). „Cały system” go nie
# usuwa — to stan sprzed zmian; znacznik służy tylko do rozpoznania, że to my
# je utworzyliśmy (kontrola po wykonaniu, gdy id portu zostało użyte ponownie).
RESTORED_KEY = "warpipe.restored"

# Pola tożsamości aplikacji, które uzupełniamy z właściwości klienta,
# jeśli węzeł ich nie ma (np. Minecraft/OpenAL: węzeł "java" bez application.name).
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
    props: dict = field(repr=False)          # właściwości węzła uzupełnione o pola klienta
    own_props: dict = field(repr=False)      # tylko właściwości węzła
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
        """Tytuł strumienia (np. karta przeglądarki). Tylko do podpowiedzi w UI —
        nigdy nie zapisywać do logów ani konfiguracji."""
        return _nonempty_str(self.own_props.get("media.name"))

    @property
    def has_pid(self) -> bool:
        """Czy SAM węzeł ma application.process.id. Discord czyta to pole z listy
        sink-input pipewire-pulse (właściwości węzła, nie klienta); bez niego
        strumień jest pomijany (np. Minecraft/OpenAL na natywnym PipeWire)."""
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
        """Wyjścia bez portów monitora (te są tylko kopią wejścia)."""
        return [p for p in self.out_ports if not p.monitor]

    def mute_state(self) -> bool:
        """True, jeśli węzeł jest wyciszony (Props.mute / softMute / zerowe softVolumes)."""
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
        """Bezpieczna etykieta do logów (bez media.name)."""
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
        """Utworzone przez to narzędzie (nasze dodatkowe albo odtworzony oryginał)."""
        return MANAGED_KEY in self.props or RESTORED_KEY in self.props


def _instance_id(info: dict) -> str | None:
    """Tożsamość bieżącego uruchomienia demona PipeWire (Core id 0).

    object.serial to licznik demona — po restarcie PipeWire zaczyna się od nowa,
    więc serial z migawki może wskazywać zupełnie inny port. cookie jest losowane
    przy każdym starcie demona; dokładamy PID demona dla pewności.
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
        """Rozwiązuje target.object: liczba = object.serial (lub id), tekst = node.name/object.path."""
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
