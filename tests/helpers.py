"""Pomocnicze: budowanie syntetycznych zrzutów pw-dump i atrapa adaptera pw."""

from __future__ import annotations

import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SAMPLE_NOAUDIO = os.path.join(FIXTURES, "sample-kde-screenshare-noaudio.json")
# Go Live Z DŹWIĘKIEM (prawdziwy zrzut z KDE/Wayland, zanonimizowany): discord_capture #179 (serial 1273,
# target.object 1272 = Firefox #221), mikrofon rozmowy recStream #93, playStream #191.
SAMPLE_GOLIVE = os.path.join(FIXTURES, "sample-kde-golive-capture.json")

# Wtyczka OpenDeck (czyste moduły warpipe_plugin bez pętli zdarzeń Qt). Bez __pycache__
# w katalogu wtyczki — ten katalog bywa kopiowany wprost do ~/.config/opendeck/plugins.
sys.dont_write_bytecode = True
PLUGIN_DIR = os.path.join(ROOT, "opendeck-plugin", "io.github.gawson.warpipe.sdPlugin")
PLUGIN_LIB = os.path.join(PLUGIN_DIR, "lib")
if PLUGIN_LIB not in sys.path:
    sys.path.insert(0, PLUGIN_LIB)

from warpipe.graph import (MANAGED_KEY, RESTORED_KEY, T_CLIENT,  # noqa: E402
                                  T_CORE, T_LINK, T_NODE, T_PORT)
from warpipe.pw import PwError  # noqa: E402


def load_fixture(path: str = SAMPLE_NOAUDIO) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class NodeRef:
    def __init__(self, id: int, serial: int, name: str):
        self.id = id
        self.serial = serial
        self.name = name
        self.ports: dict[str, int] = {}

    def p(self, name: str) -> int:
        return self.ports[name]

    def __repr__(self):  # pragma: no cover
        return f"NodeRef({self.id}, {self.name})"


class GraphBuilder:
    """Buduje listę obiektów w formacie pw-dump."""

    def __init__(self, objs: list | None = None, start_id: int = 1000, start_serial: int = 50000):
        self.objs: list[dict] = copy.deepcopy(objs) if objs else []
        self._id = start_id
        self._serial = start_serial

    def _new(self) -> tuple[int, int]:
        i, s = self._id, self._serial
        self._id += 1
        self._serial += 1
        return i, s

    # ---------------------------------------------------------------- obiekty
    def core(self, cookie: int, pid: int = 3027) -> None:
        """Obiekt Core (id 0) jak w prawdziwym pw-dump; cookie = tożsamość uruchomienia demona."""
        self.objs = [o for o in self.objs if o.get("type") != T_CORE]
        self.objs.insert(0, {"id": 0, "type": T_CORE, "info": {
            "cookie": cookie, "user-name": "tester", "host-name": "testhost", "version": "1.6.11",
            "name": "pipewire-0", "change-mask": ["props"],
            "props": {"application.process.id": pid, "core.name": f"pipewire-tester-{pid}",
                      "core.daemon": True, "object.serial": 0, "config.name": "pipewire.conf"}}})

    def client(self, app_name: str | None, binary: str | None, api: str | None = "pipewire-pulse",
               pid: int | None = 4242, **extra) -> int:
        cid, ser = self._new()
        props = {"object.serial": ser, "pipewire.access": "unrestricted"}
        if app_name is not None:
            props["application.name"] = app_name
        if binary is not None:
            props["application.process.binary"] = binary
        if api:
            props["client.api"] = api
        if pid is not None:
            props["application.process.id"] = pid
        props.update(extra)
        self.objs.append({"id": cid, "type": T_CLIENT, "info": {"props": props}})
        return cid

    def node(self, media_class: str | None, node_name: str, *, client: int | None = None,
             props: dict | None = None, ins=(), outs=(), in_prefix="input", out_prefix="output",
             monitor_outs=(), params: dict | None = None) -> NodeRef:
        nid, ser = self._new()
        p = {"node.name": node_name, "object.serial": ser, "object.id": nid}
        if media_class:
            p["media.class"] = media_class
        if client is not None:
            p["client.id"] = client
        if props:
            p.update(props)
        self.objs.append({"id": nid, "type": T_NODE, "info": {
            "props": p, "params": params or {}, "state": "running",
            "change-mask": ["props"]}})
        ref = NodeRef(nid, ser, node_name)
        idx = 0
        for ch in ins:
            ref.ports[f"{in_prefix}_{ch}"] = self._port(nid, f"{in_prefix}_{ch}", ch, "input", False, idx)
            idx += 1
        for ch in outs:
            ref.ports[f"{out_prefix}_{ch}"] = self._port(nid, f"{out_prefix}_{ch}", ch, "output", False, idx)
            idx += 1
        for ch in monitor_outs:
            ref.ports[f"monitor_{ch}"] = self._port(nid, f"monitor_{ch}", ch, "output", True, idx)
            idx += 1
        return ref

    def _port(self, node_id: int, name: str, ch: str | None, direction: str, monitor: bool, idx: int) -> int:
        pid, ser = self._new()
        props = {"node.id": node_id, "port.name": name, "object.serial": ser, "port.id": idx,
                 "port.direction": "in" if direction == "input" else "out"}
        if ch is not None and ch.isalpha():
            props["audio.channel"] = ch
        if monitor:
            props["port.monitor"] = True
        self.objs.append({"id": pid, "type": T_PORT, "info": {"direction": direction, "props": props}})
        return pid

    def sink(self, name: str, media_class="Audio/Sink", channels=("FL", "FR")) -> NodeRef:
        return self.node(media_class, name, ins=channels, in_prefix="playback", monitor_outs=channels)

    def source(self, name: str, channels=("FL", "FR"), media_class="Audio/Source") -> NodeRef:
        return self.node(media_class, name, outs=channels, out_prefix="capture")

    def app(self, app_name: str | None, binary: str | None = None, *, node_name: str | None = None,
            channels=("FL", "FR"), pid: bool = True, media_name: str = "Playback",
            client_app_name: str | None = None, api: str = "pipewire-pulse", extra: dict | None = None) -> NodeRef:
        cid = self.client(client_app_name if client_app_name is not None else app_name,
                          binary, api=api, pid=4000 + self._id if pid else None)
        props = {"media.name": media_name}
        if app_name is not None:
            props["application.name"] = app_name
        if binary is not None:
            props["application.process.binary"] = binary
        if pid:
            props["application.process.id"] = 4000 + self._id
        if extra:
            props.update(extra)
        return self.node("Stream/Output/Audio", node_name or app_name or binary or "stream",
                         client=cid, props=props, outs=channels)

    def capture(self, node_name="discord_capture", app_name="discord_capture", binary="Discord",
                media_name="game capture", target=None, channels=("FL", "FR"),
                capture_sink: bool | None = None, client: int | None = None, extra: dict | None = None,
                params: dict | None = None) -> NodeRef:
        if client is None:
            client = self.client(app_name, binary, **{"pipewire.access": "flatpak"})
        props = {"media.name": media_name, "client.api": "pipewire-pulse"}
        if app_name is not None:
            props["application.name"] = app_name
        if binary is not None:
            props["application.process.binary"] = binary
        if target is not None:
            props["target.object"] = target
        if capture_sink is not None:
            props["stream.capture.sink"] = capture_sink
        if extra:
            props.update(extra)
        return self.node("Stream/Input/Audio", node_name, client=client, props=props,
                         ins=channels, monitor_outs=channels, params=params)

    def add_port(self, node: NodeRef, name: str, ch: str | None, direction: str = "input",
                 port_id: int | None = None) -> int:
        """Port o zadanym id (np. ponownie użyty numer po zniknięciu innego portu)."""
        if port_id is None:
            return self._port(node.id, name, ch, direction, False, len(node.ports))
        _, ser = self._new()
        props = {"node.id": node.id, "port.name": name, "object.serial": ser, "port.id": len(node.ports),
                 "port.direction": "in" if direction == "input" else "out"}
        if ch:
            props["audio.channel"] = ch
        self.objs.append({"id": port_id, "type": T_PORT, "info": {"direction": direction, "props": props}})
        node.ports[name] = port_id
        return port_id

    def link(self, out_port: int, in_port: int, managed: bool = False, props: dict | None = None) -> int:
        lid, ser = self._new()
        onode = self._port_node(out_port)
        inode = self._port_node(in_port)
        p = {"object.serial": ser, "link.output.port": out_port, "link.input.port": in_port,
             "link.output.node": onode, "link.input.node": inode}
        if managed:
            p[MANAGED_KEY] = True
        if props:
            p.update(props)
        self.objs.append({"id": lid, "type": T_LINK, "info": {
            "output-node-id": onode, "output-port-id": out_port,
            "input-node-id": inode, "input-port-id": in_port,
            "state": "active", "props": p}})
        return lid

    def _port_node(self, port_id: int) -> int:
        for o in self.objs:
            if o.get("id") == port_id and o.get("type") == T_PORT:
                return o["info"]["props"]["node.id"]
        raise KeyError(port_id)

    def port_obj(self, port_id: int) -> dict | None:
        for o in self.objs:
            if o.get("id") == port_id and o.get("type") == T_PORT:
                return o
        return None

    def connect(self, src: NodeRef, dst: NodeRef, src_prefix: str | None = None,
                dst_prefix: str = "input") -> list[int]:
        """Łączy porty o tych samych kanałach (jak WirePlumber)."""
        out = []
        for sname, sid in src.ports.items():
            if src_prefix and not sname.startswith(src_prefix + "_"):
                continue
            if not src_prefix and not (sname.startswith("output_") or sname.startswith("capture_")):
                continue
            ch = sname.rsplit("_", 1)[1]
            dname = f"{dst_prefix}_{ch}"
            if dname in dst.ports:
                out.append(self.link(sid, dst.ports[dname]))
        return out

    def find_link(self, out_port: int, in_port: int) -> dict | None:
        for o in self.objs:
            if o.get("type") == T_LINK and o["info"]["output-port-id"] == out_port and o["info"]["input-port-id"] == in_port:
                return o
        return None

    def remove_link(self, out_port: int, in_port: int) -> bool:
        o = self.find_link(out_port, in_port)
        if o is None:
            return False
        self.objs.remove(o)
        return True

    def remove_node(self, ref_or_id) -> None:
        nid = ref_or_id.id if isinstance(ref_or_id, NodeRef) else ref_or_id
        ports = {o["id"] for o in self.objs if o.get("type") == T_PORT and o["info"]["props"].get("node.id") == nid}
        self.objs = [o for o in self.objs if not (
            (o.get("id") == nid and o.get("type") == T_NODE)
            or o.get("id") in ports
            or (o.get("type") == T_LINK and (o["info"]["output-node-id"] == nid or o["info"]["input-node-id"] == nid))
        )]

    def set_node_props(self, ref_or_id, **props) -> None:
        nid = ref_or_id.id if isinstance(ref_or_id, NodeRef) else ref_or_id
        for o in self.objs:
            if o.get("id") == nid and o.get("type") == T_NODE:
                o["info"]["props"].update(props)

    def links_into(self, node: NodeRef) -> set[tuple[int, int]]:
        return {(o["info"]["output-port-id"], o["info"]["input-port-id"]) for o in self.objs
                if o.get("type") == T_LINK and o["info"]["input-node-id"] == node.id}

    def all_link_pairs(self) -> set[tuple[int, int]]:
        return {(o["info"]["output-port-id"], o["info"]["input-port-id"]) for o in self.objs if o.get("type") == T_LINK}

    def dump(self) -> list:
        return copy.deepcopy(self.objs)


class FakePw:
    """Atrapa PwCli działająca na GraphBuilder."""

    def __init__(self, builder: GraphBuilder):
        self.b = builder
        self.calls: list[tuple] = []
        self.fail_link = False
        self.fail_dump = False
        self.after_op = None  # callback(fake) — np. „WirePlumber” odtwarzający połączenia
        self.before_link = None  # callback(fake, out_port, in_port) — np. zmiana grafu tuż przed pw-link

    def dump(self) -> list:
        self.calls.append(("dump",))
        if self.fail_dump:
            raise PwError("atrapa: pw-dump nie działa")
        return self.b.dump()

    def link(self, out_port: int, in_port: int, managed: bool = True) -> str:
        self.calls.append(("link", out_port, in_port, managed))
        if self.fail_link:
            raise PwError("atrapa: pw-link nie działa")
        if self.before_link:
            self.before_link(self, out_port, in_port)
        op = self.b.port_obj(out_port)
        ip = self.b.port_obj(in_port)
        if op is None or ip is None or op["info"]["direction"] != "output" or ip["info"]["direction"] != "input":
            raise PwError(f"atrapa: brak portów {out_port} -> {in_port}")
        if self.b.find_link(out_port, in_port):
            return "exists"
        # jak PwCli: nasze dodatkowe = MANAGED_KEY, odtworzony oryginał = RESTORED_KEY
        self.b.link(out_port, in_port, managed=managed, props=None if managed else {RESTORED_KEY: "true"})
        if self.after_op:
            self.after_op(self)
        return "created"

    def managed_links(self) -> list[dict]:
        return [o for o in self.b.objs if o.get("type") == T_LINK and MANAGED_KEY in o["info"]["props"]]

    def unlink(self, out_port: int, in_port: int) -> str:
        self.calls.append(("unlink", out_port, in_port))
        r = "deleted" if self.b.remove_link(out_port, in_port) else "missing"
        if self.after_op:
            self.after_op(self)
        return r

    def start_monitor(self):
        raise PwError("atrapa: brak monitora")

    @property
    def mutations(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("link", "unlink")]
