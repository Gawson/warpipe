"""Pure logic: detecting Discord's capture streams, listing apps, planning links.

Input: a Graph (from pw-dump), the detection config, the selected app keys.
Output: a list of operations (link/unlink) for the pw adapter to execute.

Nothing here touches PipeWire, so everything can be tested on snapshots.

How the official Discord client works (Linux, 2025-2026): for every app
playback stream (a sink-input with application.process.id) it opens, through
pipewire-pulse, a separate recording stream "discord_capture" / "game capture"
with target.object = the object.serial of that stream. WirePlumber then links
the app's outputs directly to that stream's inputs ("per-app" mode). Other
clients (or future versions) may read an audio output monitor instead
("monitor" mode). The planner handles both: the group of capture nodes must
receive ONLY the outputs of the selected apps on its inputs, each selected app
exactly once.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .graph import Graph, Link, Node, Port, _as_int
from .i18n import N_, _, ngettext

DEFAULT_MATCH_TERMS = ("discord", "vesktop", "vencord", "venmic", "webrtc")
# Names that unambiguously mean the stream (Go Live) audio capture.
DEFAULT_CAPTURE_NAMES = ("discord_capture", "game capture")
# Names that mean the call microphone — never pick these.
DEFAULT_MIC_NAMES = ("WEBRTC VoiceEngine", "recStream")

VESKTOP_SHARE_NODE = "vencord-screen-share"
EXCLUDED_APP_NODE_NAMES = ("vencord-sink", "vencord-screen-share")
EXCLUDED_APP_NODE_PREFIXES = ("venmic-loopback",)

IDENTITY_FIELDS = (
    "application.process.binary",
    "application.name",
    "node.name",
    "application.id",
    "pipewire.access.portal.app_id",
)

KIND_SCREENSHARE = "screenshare"
KIND_MIC = "mic"
KIND_UNKNOWN = "unknown"

KIND_LABEL = {
    KIND_SCREENSHARE: N_("stream audio"),
    KIND_MIC: N_("microphone (skipped)"),
    KIND_UNKNOWN: N_("unknown"),
}

STATUS_FOUND = "found"
STATUS_NONE = "none"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_INVALID = "invalid"

STATUS_LABEL = {
    STATUS_FOUND: N_("Discord Go Live audio: active"),
    STATUS_NONE: N_("Discord Go Live audio: not detected"),
    STATUS_AMBIGUOUS: N_("Discord Go Live audio: ambiguous — pick one in the menu"),
    STATUS_INVALID: N_("Discord Go Live audio: invalid node choice"),
}
STATUS_UNKNOWN_LABEL = N_("Discord Go Live audio: ?")


def status_label(status: str) -> str:
    """Translated one-line description of a detection status."""
    return _(STATUS_LABEL.get(status, STATUS_UNKNOWN_LABEL))


@dataclass(frozen=True)
class DetectConfig:
    match_terms: tuple[str, ...] = DEFAULT_MATCH_TERMS
    capture_names: tuple[str, ...] = DEFAULT_CAPTURE_NAMES
    mic_names: tuple[str, ...] = DEFAULT_MIC_NAMES
    # "Monitor-only" streams confirmed by the user: (program, node.name, output sink).
    confirmed: tuple[tuple[str, str, str], ...] = ()


# --------------------------------------------------------------------------
# App identity
# --------------------------------------------------------------------------

def _is_generic_loader(name: str | None) -> bool:
    if not name:
        return False
    base = name.strip().rsplit("/", 1)[-1].casefold()
    if base.startswith("wine") or base.endswith("-preloader"):
        return True
    if base.startswith("python") or base.startswith("electron"):
        return True
    return base in ("java", "javaw", "node", "mono", "dotnet")


def app_key(node: Node) -> str:
    """App identity key (stored in the config).

    application.name -> application.process.binary -> node.name.
    The binary is often a generic "loader" (wine64-preloader, java, python3,
    electron, or an *.exe run by wine) — so application.name wins (for Proton
    games it is usually the .exe name). When application.name itself is a
    loader name and node.name is more specific, node.name is used.
    """
    an, bn, nn = node.app_name, node.binary, (node.name or None)
    if an and _is_generic_loader(an) and nn and nn != an and not _is_generic_loader(nn):
        return nn
    for v in (an, bn, nn):
        if v:
            return v
    return f"#{node.id}"


def app_display(node: Node) -> str:
    return app_key(node)


def _matches_terms(node: Node, terms: Iterable[str]) -> bool:
    terms = [t.casefold() for t in terms if t]
    if not terms:
        return False
    values = []
    for k in IDENTITY_FIELDS:
        for src in (node.props, node.client_props):
            v = src.get(k)
            if v not in (None, ""):
                values.append(str(v).casefold())
    return any(t in v for v in values for t in terms)


def is_discordish(node: Node, cfg: DetectConfig) -> bool:
    """Does the node belong to a Discord client (for capture DETECTION — terms from the config)."""
    return _matches_terms(node, cfg.match_terms)


def is_discord_own_stream(node: Node, cfg: DetectConfig) -> bool:
    """Does the playback stream belong to Discord (e.g. playStream = voices from the call)?

    Independent of narrowing capture_match_terms in the config: the default
    terms always apply, and microphone/capture names exclude a stream too.
    Such streams are never shown or linked.
    """
    if _matches_terms(node, tuple(DEFAULT_MATCH_TERMS) + tuple(cfg.match_terms)):
        return True
    own = {str(v).casefold() for v in (node.name, node.app_name,
                                        node.client_props.get("application.name")) if v}
    banned = {n.casefold() for n in (*DEFAULT_MIC_NAMES, *DEFAULT_CAPTURE_NAMES,
                                     *cfg.mic_names, *cfg.capture_names) if n}
    return bool(own & banned)


def _names_lc(node: Node) -> set[str]:
    out = set()
    for v in (node.name, node.app_name, node.media_name, node.client_props.get("application.name")):
        if v:
            out.add(str(v).casefold())
    return out


# --------------------------------------------------------------------------
# Capture node detection
# --------------------------------------------------------------------------

# Detection reasons: English msgids (stable, also in --list --json); translate
# with _() / Candidate.reasons_text() when showing them.
R_TARGET_APP = N_("target: app stream")
R_TARGET_SINK = N_("target: audio output (monitor)")
R_TARGET_VSS = N_("target: vencord-screen-share")
R_CAPTURE_SINK = "stream.capture.sink"
R_KNOWN = N_("recognized earlier")
R_IN_MONITOR = N_("input from an output monitor")
R_IN_APP = N_("input from an app stream")
R_IN_VSS = N_("input from vencord-screen-share")
R_TARGET_MIC = N_("target: microphone/source")
R_IN_MIC = N_("input from a microphone/source")
R_MIC_NAME = N_("named like the call microphone")
R_CAPTURE_NAME = N_("named discord_capture/game capture")
R_NOTHING = N_("no links and no target")

# Weak evidence: stream audio reading an output monitor looks exactly like a
# MICROPHONE set to a monitor (virtual mic, soundboard). Such evidence alone is
# not enough for an automatic choice — the user has to confirm it once.
WEAK_REASONS = frozenset({R_TARGET_SINK, R_CAPTURE_SINK, R_IN_MONITOR})


@dataclass
class Candidate:
    node: Node
    kind: str
    strong: bool
    reasons: list[str]
    own_app: Node | None = None
    mic_linked: bool = False
    muted: bool = False
    weak: bool = False                          # stream audio only by monitor/sink-target evidence
    sinks: list[str] = field(default_factory=list)  # output sinks whose monitor it reads

    @property
    def id(self) -> int:
        return self.node.id

    @property
    def serial(self) -> int:
        return self.node.serial

    def signatures(self) -> list[tuple[str, str, str]]:
        """Confirmation keys: (program, node.name, output sink) — no media.name."""
        who = self.node.binary or self.node.app_name or ""
        return [(who, self.node.name, s) for s in self.sinks]

    def confirmed_by(self, cfg: "DetectConfig") -> bool:
        sigs = self.signatures()
        return bool(sigs) and all(s in cfg.confirmed for s in sigs)

    @property
    def needs_confirm(self) -> bool:
        return self.kind == KIND_SCREENSHARE and not self.strong and self.weak

    def summary(self) -> str:
        """Description without media.name — safe to log."""
        who = self.node.binary or self.node.app_name or "?"
        own = _(", app: {app}").format(app=app_key(self.own_app)) if self.own_app else ""
        return (f"#{self.node.id} {self.node.name or '?'} [{who}] — "
                f"{_(KIND_LABEL[self.kind])}{own}")

    def reasons_text(self, limit: int | None = None) -> str:
        """Translated, comma-separated detection reasons."""
        text = ", ".join(_(r) for r in self.reasons)
        return text[:limit] if limit else text


def own_app_of(graph: Graph, node: Node) -> Node | None:
    """The app stream this node records (target.object = the stream's serial)."""
    tgt = node.target_object
    if tgt is None:
        return None
    if _as_int(tgt) is None:
        return None
    t = graph.resolve_target(tgt)
    if t is not None and t.media_class == "Stream/Output/Audio":
        return t
    return None


def classify(graph: Graph, node: Node, cfg: DetectConfig, known: bool = False) -> Candidate:
    """Classify a Discord recording node.

    known=True: this node was already recognized as stream audio earlier (in
    this session, or it is in the snapshot). This matters in "monitor" mode:
    after our change the only evidence left is our own links, and when the
    selected app quits the node is left with no links at all.
    """
    names = _names_lc(node)
    strong = any(n.casefold() in names for n in cfg.capture_names if n)
    mic_marker = any(n.casefold() in names for n in cfg.mic_names if n)
    pos: list[str] = []
    neg: list[str] = []
    mic_linked = False

    sinks: list[str] = []
    own_app = own_app_of(graph, node)
    target = graph.resolve_target(node.target_object)
    if target is not None:
        mc = target.media_class
        if mc == "Stream/Output/Audio":
            pos.append(R_TARGET_APP)
        elif mc.startswith("Audio/Sink"):
            pos.append(R_TARGET_SINK)
            sinks.append(target.name)
        elif target.name == VESKTOP_SHARE_NODE:
            pos.append(R_TARGET_VSS)
        elif mc.startswith("Audio/Source"):
            if not node.capture_sink:
                neg.append(R_TARGET_MIC)
    if node.capture_sink:
        pos.append(R_CAPTURE_SINK)
    if known:
        pos.append(R_KNOWN)

    for l in graph.links_into(node.id):
        src = graph.nodes.get(l.out_node)
        sport = graph.ports.get(l.out_port)
        if src is None:
            continue
        mc = src.media_class
        if sport is not None and sport.monitor:
            pos.append(R_IN_MONITOR)
            sinks.append(src.name)
        elif mc == "Stream/Output/Audio":
            pos.append(R_IN_APP)
        elif src.name == VESKTOP_SHARE_NODE:
            pos.append(R_IN_VSS)
        elif mc.startswith("Audio/Source"):
            neg.append(R_IN_MIC)
            mic_linked = True

    pos = list(dict.fromkeys(pos))
    neg = list(dict.fromkeys(neg))
    weak = bool(pos) and all(r in WEAK_REASONS for r in pos)
    if mic_marker:
        kind = KIND_MIC
        reasons = [R_MIC_NAME] + neg + pos
    elif strong:
        kind = KIND_SCREENSHARE
        reasons = [R_CAPTURE_NAME] + pos + neg
    elif pos and not neg:
        kind = KIND_SCREENSHARE
        reasons = pos
    elif neg and not pos:
        kind = KIND_MIC
        reasons = neg
    else:
        kind = KIND_UNKNOWN
        reasons = (pos + neg) or [R_NOTHING]
    return Candidate(
        node=node, kind=kind, strong=strong and not mic_marker, reasons=reasons,
        own_app=own_app, mic_linked=mic_linked, muted=node.mute_state(),
        weak=weak, sinks=sorted(set(sinks)),
    )


@dataclass
class Detection:
    status: str
    group: list[Candidate] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def group_nodes(self) -> list[Node]:
        return [c.node for c in self.group]

    @property
    def found(self) -> bool:
        return self.status == STATUS_FOUND and bool(self.group)


def capture_candidates(graph: Graph, cfg: DetectConfig, known_serials: Iterable[int] = ()) -> list[Candidate]:
    known = set(known_serials)
    out = []
    for n in sorted(graph.nodes.values(), key=lambda n: n.serial):
        if n.media_class == "Stream/Input/Audio" and is_discordish(n, cfg):
            out.append(classify(graph, n, cfg, known=n.serial in known))
    return out


def detect(graph: Graph, cfg: DetectConfig, override_serials: Iterable[int] | None = None,
           known_serials: Iterable[int] = ()) -> Detection:
    """Pick the group of capture nodes.

    override_serials — manual choice (CLI --capture / menu); known_serials —
    nodes recognized earlier (see classify).
    """
    cands = capture_candidates(graph, cfg, known_serials)
    override = list(override_serials or [])

    if override:
        group: list[Candidate] = []
        problems: list[str] = []
        for ser in override:
            c = next((c for c in cands if c.serial == ser), None)
            if c is None:
                n = graph.node_by_serial.get(ser)
                if n is not None and n.media_class == "Stream/Input/Audio":
                    c = classify(graph, n, cfg)
            if c is None:
                problems.append(_("node with serial {serial} does not exist or is not a recording stream")
                                .format(serial=ser))
            elif c.kind == KIND_MIC:
                problems.append(_("#{id} looks like a microphone — leaving it alone").format(id=c.id))
            else:
                group.append(c)
        if problems:
            return Detection(STATUS_INVALID, [], cands, "; ".join(problems))
        det = Detection(STATUS_FOUND, group, cands, _("chosen manually"))
    else:
        strong = [c for c in cands if c.strong and c.kind == KIND_SCREENSHARE]
        generic = [c for c in cands if not c.strong and c.kind == KIND_SCREENSHARE]
        # Monitor-only evidence = it could be a microphone set to a monitor; without
        # an earlier confirmation from the user we don't pick it automatically.
        auto = [c for c in generic if not c.needs_confirm or c.confirmed_by(cfg)]
        unknown = [c for c in cands if c.kind == KIND_UNKNOWN]
        if strong:
            det = Detection(STATUS_FOUND, strong, cands,
                            ngettext("{n} discord_capture stream", "{n} discord_capture streams",
                                     len(strong)).format(n=len(strong)))
        elif len(auto) == 1:
            det = Detection(STATUS_FOUND, auto, cands, _("stream recognized by its links"))
        elif generic and not auto:
            det = Detection(STATUS_AMBIGUOUS, [], cands,
                            _("the Discord stream reads an output monitor — it could also be a microphone; "
                              "confirm it once in the menu (or with --capture ID)"))
            det.warnings.append(_("The Discord stream reads an output monitor — it could be a microphone. "
                                  "If it is the stream audio, pick it once under “Which Discord stream?”."))
        elif generic or unknown:
            det = Detection(STATUS_AMBIGUOUS, [], cands,
                            _("can't tell which Discord stream carries the stream audio — pick one manually"))
        else:
            det = Detection(STATUS_NONE, [], cands,
                            _("no stream audio capture (Go Live without sound, or not streaming)"))

    for c in det.group:
        if c.mic_linked:
            det.warnings.append(_("Warning: a microphone/source is connected to the stream audio (#{id}).")
                                .format(id=c.id))
        if c.muted:
            det.warnings.append(_("Warning: the stream audio capture (#{id}) is muted — viewers will hear nothing.")
                                .format(id=c.id))
    return det


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------

def app_streams(graph: Graph, cfg: DetectConfig) -> list[Node]:
    """Playback streams that can be selected (without Discord and venmic nodes)."""
    out = []
    for n in sorted(graph.nodes.values(), key=lambda n: n.serial):
        if n.media_class != "Stream/Output/Audio":
            continue
        if is_discord_own_stream(n, cfg):
            continue
        if n.name in EXCLUDED_APP_NODE_NAMES or n.name.startswith(EXCLUDED_APP_NODE_PREFIXES):
            continue
        out.append(n)
    return out


@dataclass
class AppEntry:
    key: str
    display: str
    nodes: list[Node]

    @property
    def has_pid(self) -> bool:
        return any(n.has_pid for n in self.nodes)

    def hints(self) -> list[str]:
        """Stream titles — ONLY for showing in the UI, never for logs/config."""
        return [n.media_name for n in self.nodes if n.media_name]


def list_apps(graph: Graph, cfg: DetectConfig) -> list[AppEntry]:
    groups: dict[str, AppEntry] = {}
    for n in app_streams(graph, cfg):
        k = app_key(n)
        e = groups.get(k.casefold())
        if e is None:
            groups[k.casefold()] = AppEntry(k, app_display(n), [n])
        else:
            e.nodes.append(n)
    return sorted(groups.values(), key=lambda e: e.display.casefold())


def apps_in_stream(graph: Graph, group: Iterable[Node], apps: Iterable[AppEntry]) -> list[str]:
    """Keys of apps whose output IS NOW linked to any node of the capture group
    (the actual graph state, not "selected")."""
    group_ids = {n.id for n in group}
    if not group_ids:
        return []
    feeding = {l.out_node for l in graph.links.values() if l.in_node in group_ids}
    return [a.key for a in apps if any(n.id in feeding for n in a.nodes)]


# --------------------------------------------------------------------------
# Channel matching
# --------------------------------------------------------------------------

LEFT = {"FL", "RL", "SL", "FLC", "RLC", "TFL", "TRL", "FLW", "FLH", "TSL", "BLC", "LLFE"}
RIGHT = {"FR", "RR", "SR", "FRC", "RRC", "TFR", "TRR", "FRW", "FRH", "TSR", "BRC", "RLFE"}
CENTER = {"MONO", "FC", "LFE", "LFE2", "RC", "TC", "TFC", "TRC", "FCH", "BC", "TFLC", "TFRC"}


def _is_mono(ports: list[Port]) -> bool:
    return len(ports) == 1 or all(p.channel == "MONO" for p in ports)


def channel_pairs(outs: list[Port], ins: list[Port]) -> list[tuple[int, int]]:
    """(output port, input port) pairs.

    FL->FL, FR->FR; mono app -> all inputs; mono input <- all channels;
    unknown channels -> port order.
    """
    if not outs or not ins:
        return []
    pairs: list[tuple[int, int]] = []
    if _is_mono(ins):
        pairs = [(o.id, i.id) for i in ins for o in outs]
    elif _is_mono(outs):
        pairs = [(o.id, i.id) for o in outs for i in ins]
    else:
        by_ch = {i.channel: i for i in ins if i.channel}
        if by_ch and all(o.channel for o in outs):
            for idx, o in enumerate(outs):
                if o.channel in by_ch:
                    pairs.append((o.id, by_ch[o.channel].id))
                elif o.channel in LEFT and "FL" in by_ch:
                    pairs.append((o.id, by_ch["FL"].id))
                elif o.channel in RIGHT and "FR" in by_ch:
                    pairs.append((o.id, by_ch["FR"].id))
                elif o.channel in CENTER:
                    pairs.extend((o.id, i.id) for i in ins)
                else:
                    pairs.append((o.id, ins[idx % len(ins)].id))
        else:
            n = max(len(outs), len(ins))
            pairs = [(outs[k % len(outs)].id, ins[k % len(ins)].id) for k in range(n)]
    return list(dict.fromkeys(pairs))


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

@dataclass
class Op:
    kind: str            # "link" | "unlink"
    out_port: int
    in_port: int
    out_node: int
    in_node: int
    link_id: int | None = None
    managed: bool = False  # link: create with our marker; unlink: the link was ours
    desc: str = ""         # no media.name
    # object.serial of the ports at planning time — PipeWire reuses port ids,
    # so after executing we check that the link ended up where it was meant to.
    out_serial: int | None = None
    in_serial: int | None = None
    original: bool = False  # link: recreating a link from the snapshot (no marker)


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)
    desired: set[tuple[int, int]] = field(default_factory=set)
    routes: list[tuple[int, int]] = field(default_factory=list)  # (app node, capture node)
    notes: list[str] = field(default_factory=list)

    @property
    def links(self) -> list[Op]:
        return [o for o in self.ops if o.kind == "link"]

    @property
    def unlinks(self) -> list[Op]:
        return [o for o in self.ops if o.kind == "unlink"]

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return bool(self.ops)


def port_label(graph: Graph, port_id: int) -> str:
    p = graph.ports.get(port_id)
    if p is None:
        return f"port#{port_id}"
    n = graph.nodes.get(p.node_id)
    return f"{(n.name if n else '?') or '?'}#{p.node_id}:{p.name}"


def _op_desc(graph: Graph, kind: str, out_port: int, in_port: int) -> str:
    arrow = "->" if kind == "link" else "-x->"
    return f"{port_label(graph, out_port)} {arrow} {port_label(graph, in_port)}"


def _link_op(graph: Graph, pair: tuple[int, int], managed: bool, original: bool = False) -> Op:
    op, ip = graph.ports[pair[0]], graph.ports[pair[1]]
    return Op("link", pair[0], pair[1], op.node_id, ip.node_id, managed=managed,
              desc=_op_desc(graph, "link", *pair), out_serial=op.serial, in_serial=ip.serial,
              original=original)


def _unlink_op(graph: Graph, l: Link) -> Op:
    op, ip = graph.ports.get(l.out_port), graph.ports.get(l.in_port)
    return Op("unlink", l.out_port, l.in_port, l.out_node, l.in_node, link_id=l.id,
              managed=l.managed, desc=_op_desc(graph, "unlink", l.out_port, l.in_port),
              out_serial=op.serial if op else None, in_serial=ip.serial if ip else None)


def plan_apply(graph: Graph, group: list[Node], selected_keys: Iterable[str], cfg: DetectConfig,
               originals: Iterable[tuple[int, int]] = ()) -> Plan:
    """Plan for a group of capture nodes.

    originals — (output port serial, input port serial) pairs of links that
    existed before our changes (the snapshot). If they have to be created again,
    they are created WITHOUT the marker: that is the original again, not our link
    ("marked" = "remove on restore").
    """
    plan = Plan()
    originals = set(originals)
    group = sorted(group, key=lambda n: n.serial)
    if not group:
        return plan
    sel = {k.casefold() for k in selected_keys if k}
    group_ids = {c.id for c in group}
    own = {}
    for c in group:
        oa = own_app_of(graph, c)
        own[c.id] = oa.serial if oa is not None else None

    streams = [n for n in app_streams(graph, cfg)
               if n.id not in group_ids and app_key(n).casefold() in sel]
    selected_serials = {n.serial for n in streams}
    selected_ids = {n.id for n in streams}
    inbound = [l for l in graph.links.values() if l.in_node in group_ids]

    # "Carrier" node for selected apps that have no capture stream of their own
    # (e.g. Minecraft without a PID). Prefer the one that already plays that role.
    sticky = sorted(
        {l.in_node for l in inbound
         if l.out_node in selected_ids
         and own.get(l.in_node) != graph.nodes[l.out_node].serial},
        key=lambda nid: graph.nodes[nid].serial,
    )

    def rank(c: Node) -> tuple:
        o = own[c.id]
        return (0 if o is None else (1 if o in selected_serials else 2), c.serial)

    carrier: Node | None = graph.nodes[sticky[0]] if sticky else None

    desired: dict[tuple[int, int], tuple[Node, Node]] = {}
    for s in streams:
        outs = s.out_ports_audio
        if not outs:
            plan.notes.append(_("{app} (#{id}) has no output ports yet").format(app=app_key(s), id=s.id))
            continue
        own_caps = [c for c in group if own[c.id] == s.serial]
        if own_caps:
            dest = own_caps[0]
        else:
            linked_into = Counter(l.in_node for l in inbound if l.out_node == s.id)
            if linked_into:
                dest = graph.nodes[max(linked_into, key=lambda nid: (linked_into[nid], -graph.nodes[nid].serial))]
            else:
                if carrier is None:
                    carrier = sorted(group, key=rank)[0]
                dest = carrier
        if not dest.in_ports:
            plan.notes.append(_("capture stream #{id} has no input ports yet").format(id=dest.id))
            continue
        plan.routes.append((s.id, dest.id))
        for pair in channel_pairs(outs, dest.in_ports):
            desired.setdefault(pair, (s, dest))

    existing = {(l.out_port, l.in_port): l for l in inbound}
    for pair in desired:
        if pair not in existing:
            ser = (graph.ports[pair[0]].serial, graph.ports[pair[1]].serial)
            orig = ser in originals
            plan.ops.append(_link_op(graph, pair, managed=not orig, original=orig))
    for pair, l in sorted(existing.items(), key=lambda kv: kv[1].id):
        if pair not in desired:
            plan.ops.append(_unlink_op(graph, l))
    plan.desired = set(desired)
    return plan


# --------------------------------------------------------------------------
# Snapshot and restore
# --------------------------------------------------------------------------

def link_entry(graph: Graph, link: Link, deleted: bool = False) -> dict | None:
    """Snapshot entry for a link (no media.name)."""
    op = graph.ports.get(link.out_port)
    ip = graph.ports.get(link.in_port)
    if op is None or ip is None:
        return None
    on = graph.nodes.get(link.out_node)
    inn = graph.nodes.get(link.in_node)
    return {
        "out_port_serial": op.serial,
        "in_port_serial": ip.serial,
        "out_node_serial": on.serial if on else None,
        "in_node_serial": inn.serial if inn else None,
        "out_node_name": on.name if on else None,
        "in_node_name": inn.name if inn else None,
        "out_port_name": op.name,
        "in_port_name": ip.name,
        "deleted": bool(deleted),
    }


def _snapshot_captures(graph: Graph, snapshot: dict):
    """(capture node, entries) — only for nodes that still exist, are recording
    streams and have the same name as in the snapshot. A snapshot from another
    PipeWire session (different daemon cookie) is skipped entirely: serial is a
    daemon counter and after a restart points at completely different objects."""
    if not isinstance(snapshot, dict) or snapshot.get("pw_instance") != graph.instance_id:
        return
    caps = snapshot.get("captures")
    if not isinstance(caps, dict):
        return
    for key, cap in caps.items():
        if not isinstance(cap, dict):
            continue
        ser = _as_int(key)
        node = graph.node_by_serial.get(ser) if ser is not None else None
        if node is None or node.media_class != "Stream/Input/Audio":
            continue
        if cap.get("node_name") != node.name:
            continue
        links = cap.get("links")
        if isinstance(links, list):
            yield node, [e for e in links if isinstance(e, dict)]


def _entry_ports(graph: Graph, cap: Node, e: dict) -> tuple[Port, Port] | None:
    """Ports of a snapshot entry in the current graph — only when EVERYTHING matches:
    port serials and names, source node serial and name, and the input belongs
    to the same capture node."""
    op = graph.port_by_serial.get(_as_int(e.get("out_port_serial")))
    ip = graph.port_by_serial.get(_as_int(e.get("in_port_serial")))
    if op is None or ip is None or op.direction != "out" or ip.direction != "in":
        return None
    if ip.node_id != cap.id or _as_int(e.get("in_node_serial")) != cap.serial:
        return None
    if "in_node_name" in e and e.get("in_node_name") != cap.name:
        return None
    on = graph.nodes.get(op.node_id)
    if on is None or _as_int(e.get("out_node_serial")) != on.serial:
        return None
    if e.get("out_node_name") != on.name or e.get("out_port_name") != op.name \
            or e.get("in_port_name") != ip.name:
        return None
    return op, ip


def snapshot_originals(graph: Graph, snapshot: dict) -> set[tuple[int, int]]:
    """Port serial pairs of snapshot links (the originals from before our changes)."""
    out = set()
    for cap, entries in _snapshot_captures(graph, snapshot):
        for e in entries:
            pp = _entry_ports(graph, cap, e)
            if pp:
                out.add((pp[0].serial, pp[1].serial))
    return out


def plan_restore(graph: Graph, snapshot: dict) -> Plan:
    """Restore: remove our (marked) links, recreate the ones we removed.

    The order matters (execute in plan.ops order): first recreate missing links,
    then replace marked copies of originals with unmarked ones (unlink + link),
    finally remove the rest of our links.
    """
    plan = Plan()
    restorable: set[tuple[int, int]] = set()
    caps = snapshot.get("captures") if isinstance(snapshot, dict) else None
    if caps and snapshot.get("pw_instance") != graph.instance_id:
        plan.notes.append(_("the snapshot is from a different PipeWire session — not restoring links from it"))
    for cap, entries in _snapshot_captures(graph, snapshot):
        for e in entries:
            if e.get("deleted") is not True:
                continue
            pp = _entry_ports(graph, cap, e)
            if pp:
                restorable.add((pp[0].id, pp[1].id))
    existing = graph.link_pairs()
    replace: list[Link] = []
    for pair in sorted(restorable):
        l = existing.get(pair)
        if l is None:
            plan.ops.append(_link_op(graph, pair, managed=False, original=True))
        elif l.managed:
            replace.append(l)
    for l in replace:
        # A marked link that is an original. If it stayed marked, the next restore
        # (with an empty snapshot) would delete it — replace it with an unmarked one.
        plan.ops.append(_unlink_op(graph, l))
        plan.ops.append(_link_op(graph, (l.out_port, l.in_port), managed=False, original=True))
    for l in sorted(graph.links.values(), key=lambda l: l.id):
        if l.managed and (l.out_port, l.in_port) not in restorable:
            plan.ops.append(_unlink_op(graph, l))
    plan.desired = restorable
    return plan
