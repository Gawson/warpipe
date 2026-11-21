"""The Warpipe window: opened by a left click on the tray icon.

A normal top-level window, not a popup menu: on Wayland a popup (xdg_popup) needs
a parent surface and a tray icon has none, so QMenu.popup() from a tray click is
never shown. The compositor places the window (Wayland ignores positions); under
X11 it opens next to the tray icon / the cursor (place_near()). Escape or the next
left click on the tray icon hides it.

Layout: header (green pipe, "Warpipe", status pill, "Only selected apps" switch),
notices (e.g. nothing selected = no stream audio), "What the stream hears": one
row per app (icon, name, what happens to its audio, a small pipe while it is on
stream, a switch), footer (Whole system, menu button).

popup_model() turns an Enforcer Status into plain data (testable without Qt).
PopupWindow only shows that data and reports clicks through Qt signals; the
TrayController calls the Enforcer and feeds every new status back in the GUI
thread (queued signal), so the window updates live.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, DESKTOP_ID
from .i18n import _, ngettext
from .icons import (ICON_STATE_ACTIVE, DesktopIndex, app_icon_names, icon_state,
                    pipe_icon, pipe_pixmap, resolve_icon, theme_icon)
from .planner import STATUS_AMBIGUOUS, STATUS_FOUND, STATUS_INVALID, STATUS_NONE

WIDTH = 420
MASTER_LABEL_MAX = 140
MAX_VISIBLE_ROWS = 6

PILL_OK = "ok"
PILL_IDLE = "idle"
PILL_WARN = "warn"
PILL_ERROR = "error"

ROW_ON_STREAM = "on_stream"      # its audio is linked to the Go Live stream right now
ROW_WAITING = "waiting"          # selected, enforcing, no Go Live audio yet
ROW_PENDING = "pending"          # selected, Go Live audio present, not linked (yet)
ROW_NO_PID = "no_pid"            # Discord can't capture it on its own; Warpipe can add it
ROW_PLAYING = "playing"          # playing, kept off the stream
ROW_REMEMBERED = "remembered"    # selected earlier, not playing now


# ------------------------------------------------------------------ model
@dataclass(frozen=True)
class Notice:
    kind: str          # PILL_WARN / PILL_ERROR
    text: str


@dataclass(frozen=True)
class AppRow:
    key: str
    title: str
    subtitle: str
    state: str
    checked: bool
    present: bool
    on_stream: bool
    tooltip: str
    icon_names: tuple[str, ...]


@dataclass(frozen=True)
class PopupModel:
    pill: str
    pill_kind: str
    pill_tooltip: str
    enabled: bool
    icon_state: str
    notices: tuple[Notice, ...]
    rows: tuple[AppRow, ...]


def _pill(st) -> tuple[str, str]:
    if st.error and st.detection is None:
        return _("PipeWire error"), PILL_ERROR
    cs = st.capture_status
    if cs == STATUS_FOUND:
        return _("Discord Go Live: audio active"), PILL_OK
    if cs == STATUS_NONE:
        return _("No Go Live audio"), PILL_IDLE
    if cs == STATUS_AMBIGUOUS:
        return _("Capture ambiguous"), PILL_WARN
    if cs == STATUS_INVALID:
        return _("Invalid stream choice"), PILL_WARN
    return st.headline(), PILL_IDLE


def _notices(st) -> list[Notice]:
    out: list[Notice] = []
    if st.enabled and not st.selected:
        out.append(Notice(PILL_WARN, _("No app selected — stream has no audio")))
    if st.error:
        out.append(Notice(PILL_ERROR, _("Error: {error}").format(error=st.error)))
    out += [Notice(PILL_WARN, w) for w in st.warnings]
    if st.capture_status == STATUS_AMBIGUOUS and not st.warnings:
        out.append(Notice(PILL_WARN, _("Several Discord streams could be Go Live — pick one in the menu (bottom right)")))
    return out


def _row_subtitle(state: str) -> str:
    return {
        ROW_ON_STREAM: _("on stream"),
        ROW_WAITING: _("selected · waiting for Go Live"),
        ROW_PENDING: _("selected · not on stream yet"),
        ROW_NO_PID: _("Discord can't capture this — Warpipe adds it"),
        ROW_PLAYING: _("playing, not on stream"),
        ROW_REMEMBERED: _("not playing · remembered"),
    }[state]


def _shorten(s: str, n: int = 80) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def popup_model(st, desktop: DesktopIndex | None = None) -> PopupModel:
    """Everything the window shows, from an Enforcer Status."""
    pill, kind = _pill(st)
    found = st.detection is not None and st.detection.found
    selected = {k.casefold() for k in st.selected}
    in_stream = {k.casefold() for k in st.in_stream}
    rows: list[AppRow] = []
    for a in st.apps:
        cf = a.key.casefold()
        checked = cf in selected
        on = cf in in_stream
        if on:
            state = ROW_ON_STREAM
        elif checked and st.enabled:
            state = ROW_PENDING if found else ROW_WAITING
        elif not a.has_pid:
            state = ROW_NO_PID
        else:
            state = ROW_PLAYING
        n = len(a.nodes)
        title = a.display + (" (" + ngettext("{n} stream", "{n} streams", n).format(n=n) + ")" if n > 1 else "")
        # media.name only as a UI hint (never goes to logs or the config)
        tip = "\n".join([a.key] + [_shorten(h) for h in a.hints()[:5]])
        props = [p for node in a.nodes for p in (node.props, node.client_props)]
        rows.append(AppRow(a.key, title, _row_subtitle(state), state, checked, True, on, tip,
                           tuple(app_icon_names(props, a.key, desktop))))
    for key in st.missing_selected():
        rows.append(AppRow(key, key, _row_subtitle(ROW_REMEMBERED), ROW_REMEMBERED, True, False, False,
                           key, tuple(app_icon_names([], key, desktop))))
    return PopupModel(pill, kind, f"{st.headline()}\n{st.routing_text()}", st.enabled,
                      icon_state(st), tuple(_notices(st)), tuple(rows))


Rect = tuple[int, int, int, int]   # x, y, width, height


def place_near(w: int, h: int, anchor: Rect, area: Rect, gap: int = 6) -> tuple[int, int]:
    """Top-left corner for a w×h window next to `anchor` (the tray icon, or the
    cursor as a 1×1 rect), kept inside `area` (the screen's available geometry).
    Above the anchor when it is in the lower half of the area, else below it."""
    ax, ay, aw, ah = anchor
    x0, y0, W, H = area
    x = ax + aw // 2 - w // 2
    y = ay - gap - h if ay + ah // 2 > y0 + H // 2 else ay + ah + gap
    x = max(x0, min(x, x0 + W - w))
    y = max(y0, min(y, y0 + H - h))
    return x, y


# ------------------------------------------------------------------ colors
GREEN = "#3daa4f"


def is_dark(pal: QPalette) -> bool:
    return pal.color(QPalette.ColorRole.Window).lightnessF() < 0.5


def accent(kind: str, pal: QPalette) -> QColor:
    """Status colors, darker on light themes so small text keeps its contrast."""
    dark = is_dark(pal)
    table = {
        PILL_OK: ("#3daa4f", "#25803a"),
        PILL_WARN: ("#f0a030", "#a85a00"),
        PILL_ERROR: ("#f0606a", "#c0303c"),
    }
    if kind in table:
        return QColor(table[kind][0 if dark else 1])
    return mix(pal.color(QPalette.ColorRole.WindowText), pal.color(QPalette.ColorRole.Window), 0.35)


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """a blended towards b by t (0 = a, 1 = b)."""
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def rgba(c: QColor, alpha: int) -> str:
    # never #RRGGBBAA in a style sheet: Qt reads it as #AARRGGBB
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


# ------------------------------------------------------------------ widgets
class Toggle(QAbstractButton):
    """An on/off switch."""

    W, H = 40, 22

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(self.W, self.H)

    def sizeHint(self) -> QSize:
        return QSize(self.W, self.H)

    def set_checked_quietly(self, on: bool) -> None:
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        on, en = self.isChecked(), self.isEnabled()
        text = pal.color(QPalette.ColorRole.WindowText)
        if not en:
            track = mix(text, pal.color(QPalette.ColorRole.Window), 0.8)
        elif on:
            track = QColor(GREEN)
        else:
            track = QColor(text)
            track.setAlphaF(0.32)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(1, 3, self.W - 2, 16), 8, 8)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(pal.color(QPalette.ColorRole.Highlight), 1.5))
            p.drawRoundedRect(QRectF(0.5, 2.5, self.W - 1, 17), 8.5, 8.5)
        knob = QRectF(self.W - 21 if on else 1, 1, 20, 20).adjusted(1, 1, -1, -1)
        p.setBrush(QColor("#fcfcfc") if en else mix(QColor("#fcfcfc"), track, 0.4))
        p.setPen(QPen(QColor(0, 0, 0, 70), 1))
        p.drawEllipse(knob)
        p.end()


class ElidedLabel(QLabel):
    """One line of text, elided with "…" when it doesn't fit (full text in the tooltip)."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._color: QColor | None = None
        self._tip = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_color(self, color: QColor | None) -> None:
        self._color = color
        self.update()

    def set_tip(self, tip: str) -> None:
        self._tip = tip
        self.setToolTip(tip)

    def minimumSizeHint(self) -> QSize:
        return QSize(16, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        r = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, r.width())
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        p.setPen(self._color or self.palette().color(group, QPalette.ColorRole.WindowText))
        p.drawText(r, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        p.end()
        tip = self._tip or (self.text() if text != self.text() else "")
        if self.toolTip() != tip:
            self.setToolTip(tip)


class Pill(QWidget):
    """Rounded status badge with a dot; the text is elided when space runs out."""

    PAD_X, PAD_Y = 9, 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._color = QColor(GREEN)
        f = QFont(self.font())
        f.setWeight(QFont.Weight.DemiBold)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def set_pill(self, text: str, color: QColor) -> None:
        self._text, self._color = text, QColor(color)
        self.updateGeometry()
        self.update()

    def _dot(self) -> int:
        return max(6, self.fontMetrics().height() // 2 - 1)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._text) + self._dot() + 6 + 2 * self.PAD_X + 2,
                     fm.height() + 2 * self.PAD_Y + 2)

    def minimumSizeHint(self) -> QSize:
        return QSize(self.sizeHint().height() * 2, self.sizeHint().height())

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self._color
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill, line = QColor(c), QColor(c)
        fill.setAlpha(40)
        line.setAlpha(110)
        p.setPen(QPen(line, 1))
        p.setBrush(fill)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = self._dot()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(self.PAD_X, (self.height() - d) / 2, d, d))
        x = self.PAD_X + d + 6
        tr = self.rect().adjusted(x, 0, -self.PAD_X, 0)
        p.setPen(c)
        p.drawText(tr, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self.fontMetrics().elidedText(self._text, Qt.TextElideMode.ElideRight, tr.width()))
        p.end()


class AppRowWidget(QFrame):
    toggled = Signal(str, bool)

    ICON = 32

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.key = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedSize(self.ICON, self.ICON)
        lay.addWidget(self.icon)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name = ElidedLabel()
        f = QFont(self.name.font())
        f.setPointSizeF(f.pointSizeF() + 0.5)
        f.setWeight(QFont.Weight.DemiBold)
        self.name.setFont(f)
        self.sub = ElidedLabel()
        sf = QFont(self.sub.font())
        sf.setPointSizeF(sf.pointSizeF() - 0.5)
        self.sub.setFont(sf)
        col.addWidget(self.name)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.pipe = QLabel()
        self.pipe.setToolTip(_("on stream"))
        lay.addWidget(self.pipe)
        self.switch = Toggle()
        self.switch.toggled.connect(lambda on: self.toggled.emit(self.key, bool(on)))
        lay.addWidget(self.switch)

    def set_row(self, row: AppRow, icon: QIcon) -> None:
        self.key = row.key
        pal = self.palette()
        dim = accent(PILL_IDLE, pal)
        self.name.setText(row.title)
        self.name.set_tip(row.tooltip)
        self.name.set_color(None if row.present else dim)
        self.sub.setText(row.subtitle)
        self.sub.set_color(accent(PILL_OK, pal) if row.on_stream else dim)
        dpr = self.devicePixelRatioF()
        mode = QIcon.Mode.Normal if row.present else QIcon.Mode.Disabled
        self.icon.setPixmap(icon.pixmap(QSize(self.ICON, self.ICON), dpr, mode))
        if row.on_stream and self.pipe.pixmap().isNull():
            self.pipe.setPixmap(pipe_pixmap(22, ICON_STATE_ACTIVE, dpr))
        self.pipe.setVisible(row.on_stream)
        self.switch.set_checked_quietly(row.checked)
        self.switch.setAccessibleName(row.title)
        self.setToolTip(row.tooltip)


class NoticeWidget(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notice")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 6, 9, 6)
        lay.setSpacing(8)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.label, 1)
        self.kind = ""

    def set_notice(self, n: Notice) -> None:
        self.kind = n.kind
        self.label.setText(n.text)
        c = accent(n.kind, self.palette())
        self.setStyleSheet(f"QFrame#notice {{ background:{rgba(c, 34)}; border:1px solid {rgba(c, 120)};"
                           f" border-radius:6px; }}")
        icon = theme_icon(["dialog-error"] if n.kind == PILL_ERROR else ["dialog-warning", "emblem-warning"])
        self.icon.setPixmap(icon.pixmap(QSize(16, 16), self.devicePixelRatioF()))


def _drop(layout, w: QWidget) -> None:
    """Take a widget out of the window NOW. deleteLater() alone leaves it painted at
    its old place until the event loop gets to it (or forever without one), on top
    of whatever the layout puts there next."""
    w.hide()
    layout.removeWidget(w)
    w.setParent(None)
    w.deleteLater()


class PopupWindow(QWidget):
    enforce_toggled = Signal(bool)
    app_toggled = Signal(str, bool)
    whole_system_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setObjectName("warpipe-popup")
        self.setWindowTitle(APP_NAME)
        icon = pipe_icon()
        self.setWindowIcon(icon if not icon.isNull() else theme_icon([DESKTOP_ID]))
        self.setFixedWidth(WIDTH)
        self.model: PopupModel | None = None
        self.rows: list[AppRowWidget] = []
        self.notices: list[NoticeWidget] = []
        self._icons: dict[tuple[str, ...], QIcon] = {}
        self._logo_state: str | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 12)
        v.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.logo = QLabel()
        self.logo.setFixedSize(40, 40)
        head.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignVCenter)
        tbox = QVBoxLayout()
        tbox.setSpacing(3)
        self.title = QLabel(APP_NAME)
        tf = QFont(self.title.font())
        tf.setPointSizeF(tf.pointSizeF() + 4)
        tf.setWeight(QFont.Weight.Bold)
        self.title.setFont(tf)
        tbox.addWidget(self.title)
        self.pill = Pill()
        tbox.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignLeft)
        head.addLayout(tbox, 1)
        mbox = QVBoxLayout()
        mbox.setSpacing(3)
        self.master_label = QLabel()
        self.master_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.master_label.setWordWrap(True)
        self.master_label.setMaximumWidth(MASTER_LABEL_MAX)
        mbox.addWidget(self.master_label, 0, Qt.AlignmentFlag.AlignRight)
        self.master = Toggle()
        self.master.toggled.connect(lambda on: self.enforce_toggled.emit(bool(on)))
        mbox.addWidget(self.master, 0, Qt.AlignmentFlag.AlignRight)
        head.addLayout(mbox)
        v.addLayout(head)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(sep)

        self.notice_box = QVBoxLayout()
        self.notice_box.setSpacing(6)
        v.addLayout(self.notice_box)

        self.section = QLabel()
        sf = QFont(self.section.font())
        sf.setWeight(QFont.Weight.DemiBold)
        self.section.setFont(sf)
        v.addWidget(self.section)

        self.rows_widget = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("rows")
        self.scroll.setWidget(self.rows_widget)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.rows_widget.setAutoFillBackground(False)
        v.addWidget(self.scroll)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(48)
        v.addWidget(self.empty)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.system_button = QPushButton()
        self.system_button.setIcon(theme_icon(["audio-volume-high", "audio-card"]))
        self.system_button.clicked.connect(lambda: self.whole_system_clicked.emit())
        foot.addWidget(self.system_button, 1)
        self.menu_button = QToolButton()
        icon = theme_icon(["configure", "application-menu", "preferences-system"])
        if icon.isNull():
            self.menu_button.setText("☰")
        self.menu_button.setIcon(icon)
        self.menu_button.setIconSize(QSize(20, 20))
        self.menu_button.setAutoRaise(True)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        foot.addWidget(self.menu_button)
        v.addLayout(foot)

        self.retranslate()
        self._restyle()

    # -------------------------------------------------------------- texts
    def retranslate(self) -> None:
        self.master_label.setText(_("Only selected apps"))
        # one line when it fits, wrapped (right-aligned) when a translation is long
        tw = self.master_label.fontMetrics().horizontalAdvance(self.master_label.text()) + 2
        self.master_label.setMinimumWidth(min(tw, MASTER_LABEL_MAX))
        self.master.setAccessibleName(_("Only selected apps"))
        self.section.setText(_("What the stream hears"))
        self.empty.setText(_("No app is playing right now"))
        self.system_button.setText(_("Whole system"))
        self.system_button.setToolTip(_("Back to Discord's default: all system audio goes to the stream"))
        self.menu_button.setToolTip(_("Menu"))
        self.menu_button.setAccessibleName(_("Menu"))
        if self.model is not None:
            self.set_model(self.model, force=True)

    def _restyle(self) -> None:
        pal = self.palette()
        alt = pal.color(QPalette.ColorRole.AlternateBase)
        if is_dark(pal) and alt.lightnessF() >= pal.color(QPalette.ColorRole.Window).lightnessF() + 0.25:
            alt = pal.color(QPalette.ColorRole.Base)
        m = pal.color(QPalette.ColorRole.Mid)
        self.setStyleSheet(
            f"QFrame#row {{ background:{alt.name()}; border:1px solid {rgba(m, 90)}; border-radius:8px; }}"
            "QScrollArea#rows, QScrollArea#rows > QWidget > QWidget { background: transparent; }")
        dim = accent(PILL_IDLE, pal)
        self.empty.setStyleSheet(f"color:{dim.name()};")

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.PaletteChange:
            self._restyle()
            if self.model is not None:
                self.set_model(self.model, force=True)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    # -------------------------------------------------------------- model
    def _icon(self, names: tuple[str, ...]) -> QIcon:
        icon = self._icons.get(names)
        if icon is None:
            icon = self._icons[names] = resolve_icon(names)
        return icon

    def set_model(self, model: PopupModel, force: bool = False) -> None:
        old = self.model
        self.model = model
        if old == model and not force:
            return
        pal = self.palette()
        dpr = self.devicePixelRatioF()
        if self._logo_state != model.icon_state or force:
            self._logo_state = model.icon_state
            self.logo.setPixmap(pipe_pixmap(40, model.icon_state, dpr))
        self.pill.set_pill(model.pill, accent(model.pill_kind, pal))
        self.pill.setToolTip(model.pill_tooltip)
        self.master.set_checked_quietly(model.enabled)
        self.system_button.setEnabled(model.enabled)

        while len(self.notices) > len(model.notices):
            _drop(self.notice_box, self.notices.pop())
        while len(self.notices) < len(model.notices):
            w = NoticeWidget(self)
            self.notice_box.addWidget(w)
            w.show()          # now, not queued: _fit() below must measure it
            self.notices.append(w)
        for w, n in zip(self.notices, model.notices):
            w.set_notice(n)

        while len(self.rows) > len(model.rows):
            _drop(self.rows_layout, self.rows.pop())
        while len(self.rows) < len(model.rows):
            w = AppRowWidget(self.rows_widget)
            w.toggled.connect(self.app_toggled)
            self.rows_layout.addWidget(w)
            w.show()
            self.rows.append(w)
        for w, r in zip(self.rows, model.rows):
            w.set_row(r, self._icon(r.icon_names))
        for w in (*self.notices, *self.rows):
            # style sheet borders and new texts must count in the size hints measured
            # below — not only after the next event loop pass (a LayoutRequest)
            w.ensurePolished()
            w.layout().invalidate()
            w.layout().activate()      # also clears the nested layouts' cached hints
        self.scroll.setVisible(bool(model.rows))
        self.empty.setVisible(not model.rows)
        self._fit()

    def row(self, key: str) -> AppRowWidget | None:
        return next((w for w in self.rows if w.key == key), None)

    def _fit(self) -> None:
        """Rows area: as tall as its rows, up to MAX_VISIBLE_ROWS (then it scrolls);
        the window: as tall as its contents at the fixed width."""
        lay = self.rows_layout
        n = len(self.rows)
        if n:
            hs = [w.sizeHint().height() for w in self.rows]
            shown = hs[:MAX_VISIBLE_ROWS]
            self.scroll.setFixedHeight(sum(shown) + lay.spacing() * (len(shown) - 1))
        else:
            self.scroll.setFixedHeight(0)
        top = self.layout()
        top.invalidate()
        top.activate()
        h = top.heightForWidth(WIDTH) if top.hasHeightForWidth() else top.sizeHint().height()
        h = max(h, top.minimumSize().height())
        if h != self.height() or self.minimumHeight() != h or self.maximumHeight() != h:
            self.setFixedHeight(h)     # one resize per change (no intermediate sizes)
        # Place the children for the final size right away — Qt defers the resize
        # event of a hidden window, and nothing must be painted at stale positions.
        top.setGeometry(self.rect())
