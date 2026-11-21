"""Key rendering: QImage + QPainter -> PNG -> data URL for setImage.

OpenDeck draws the key image on a 144x144 canvas (and only then scales it down
to 72x72 for a Stream Deck V2); it accepts PNG/SVG data URLs. PNG works
everywhere (also in the Elgato software), so we draw a 144x144 PNG.

Simple drawn symbols (speaker, "!", "?", bars) — no third-party logos.

The PNG also carries short metadata (tEXt/iTXt): ddstate, ddlabel, ddbadge,
ddring — tests and the preview use them to check which state was drawn.

Needs an existing QGuiApplication (fonts) — the plugin creates one with -platform offscreen.
"""

from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen)

from .logic import (S_ABSENT, S_DOWN, S_ERROR, S_NOSTREAM, S_OFF, S_ON,
                    S_STARTING, S_SYS_OFF, S_SYS_ON, S_UNSET, S_UNSURE, Visual)

SIZE = 144

# background (top, bottom), symbol, caption, badge (background, text)
STYLE = {
    S_ON:       ("#22a356", "#13753a", "#ffffff", "#ffffff", "#0a3d1d", "#d7ffe4"),
    S_SYS_ON:   ("#22a356", "#13753a", "#ffffff", "#ffffff", "#0a3d1d", "#d7ffe4"),
    S_OFF:      ("#2a3646", "#161e29", "#e2e8f0", "#f1f5f9", "#0f172a", "#cbd5e1"),
    S_SYS_OFF:  ("#2a3646", "#161e29", "#94a3b8", "#f1f5f9", "#0f172a", "#cbd5e1"),
    S_NOSTREAM: ("#2a3646", "#161e29", "#9fb0c4", "#e2e8f0", "#3d2a04", "#fbbf24"),
    S_UNSURE:   ("#2a3646", "#161e29", "#9fb0c4", "#e2e8f0", "#3d2a04", "#fbbf24"),
    S_ABSENT:   ("#1a1a1d", "#0c0c0e", "#4b4b52", "#7a7a82", "#26262a", "#9a9aa2"),
    S_DOWN:     ("#4a0d0d", "#260606", "#ef4444", "#fecaca", "#7f1d1d", "#ffffff"),
    S_ERROR:    ("#4a0d0d", "#260606", "#ef4444", "#fecaca", "#7f1d1d", "#ffffff"),
    S_STARTING: ("#2a3646", "#161e29", "#fbbf24", "#e2e8f0", "#3d2a04", "#fbbf24"),
    S_UNSET:    ("#2c2c31", "#1b1b1f", "#a1a1aa", "#d4d4d8", "#18181b", "#d4d4d8"),
}
RING = "#22c55e"


def _font(px: float, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(["Noto Sans", "DejaVu Sans", "Liberation Sans", "Sans Serif"])
    f.setPixelSize(max(1, int(round(px))))
    f.setBold(bold)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


# --------------------------------------------------------------------------
# Symbols (coordinates in image pixels; s = scale relative to 144)
# --------------------------------------------------------------------------

def _speaker(p: QPainter, cx: float, cy: float, s: float, color: QColor, waves: int, slash: bool) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    x0 = cx - 34 * s
    body = QRectF(x0, cy - 11 * s, 15 * s, 22 * s)
    path = QPainterPath()
    path.addRoundedRect(body, 3 * s, 3 * s)
    cone = QPainterPath()
    cone.moveTo(x0 + 13 * s, cy - 11 * s)
    cone.lineTo(x0 + 34 * s, cy - 27 * s)
    cone.lineTo(x0 + 34 * s, cy + 27 * s)
    cone.lineTo(x0 + 13 * s, cy + 11 * s)
    cone.closeSubpath()
    p.drawPath(path.united(cone))
    pen = QPen(color, 6.5 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(waves):
        r = (13 + 11 * i) * s
        rect = QRectF(x0 + 34 * s - r + 4 * s, cy - r, 2 * r, 2 * r)
        p.drawArc(rect, -45 * 16, 90 * 16)
    if slash:
        pen = QPen(QColor(color).lighter(140), 7 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(cx - 30 * s, cy - 30 * s), QPointF(cx + 30 * s, cy + 30 * s))


def _bars(p: QPainter, cx: float, cy: float, s: float, color: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    heights = (22, 40, 56, 34, 46)
    w = 8 * s
    gap = 5 * s
    total = len(heights) * w + (len(heights) - 1) * gap
    x = cx - total / 2
    base = cy + 28 * s
    for h in heights:
        p.drawRoundedRect(QRectF(x, base - h * s, w, h * s), 3 * s, 3 * s)
        x += w + gap


def _circle_text(p: QPainter, cx: float, cy: float, s: float, circle: QColor, fg: QColor, text: str) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(circle))
    r = 27 * s
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(fg)
    p.setFont(_font(40 * s, bold=True))
    p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, text)


def _dots(p: QPainter, cx: float, cy: float, s: float, color: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    for i in (-1, 0, 1):
        p.drawEllipse(QPointF(cx + i * 20 * s, cy), 7 * s, 7 * s)


def _glyph(p: QPainter, v: Visual, cx: float, cy: float, s: float, color: QColor) -> None:
    if v.state in (S_DOWN, S_ERROR):
        _circle_text(p, cx, cy, s, color, QColor("#ffffff"), "!")
    elif v.state == S_STARTING:
        _dots(p, cx, cy, s, color)
    elif v.state == S_UNSET:
        _circle_text(p, cx, cy, s, QColor("#3f3f46"), color, "?")
    elif v.kind == "system":
        _bars(p, cx, cy, s, color)
    elif v.state == S_ON:
        _speaker(p, cx, cy, s, color, 3, False)
    elif v.state == S_ABSENT:
        _speaker(p, cx, cy, s, color, 0, True)
    else:
        _speaker(p, cx, cy, s, color, 2 if v.state == S_OFF else 1, False)


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def _fit_label(p: QPainter, text: str, rect: QRectF, color: QColor, max_px: float, min_px: float) -> None:
    if not text:
        return
    p.setPen(color)
    base = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
    px = max_px
    while px >= min_px:
        f = _font(px, bold=True)
        fm = QFontMetricsF(f)
        for wrap in (Qt.TextFlag.TextWordWrap, Qt.TextFlag.TextWrapAnywhere):
            flags = int(base) | int(wrap)
            br = fm.boundingRect(rect, flags, text)
            if br.height() <= rect.height() + 0.5 and br.width() <= rect.width() + 0.5:
                p.setFont(f)
                p.drawText(rect, flags, text)
                return
        px -= 1
    # nothing fits: smallest font, two lines with an ellipsis
    f = _font(min_px, bold=True)
    fm = QFontMetricsF(f)
    shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width() * 1.9)
    p.setFont(f)
    p.drawText(rect, int(base) | int(Qt.TextFlag.TextWrapAnywhere), shown)


def _badge(p: QPainter, text: str, s: float, size: float, bg: QColor, fg: QColor) -> None:
    if not text:
        return
    max_w = size - 10 * s
    px = 18 * s
    while True:
        f = _font(px, bold=True)
        fm = QFontMetricsF(f)
        if fm.horizontalAdvance(text) + 16 * s <= max_w or px <= 12 * s:
            break
        px -= 0.5 * s
    w = min(max_w, fm.horizontalAdvance(text) + 16 * s)
    h = 25 * s
    rect = QRectF((size - w) / 2, 7 * s, w, h)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(bg))
    p.drawRoundedRect(rect, h / 2, h / 2)
    p.setPen(fg)
    p.setFont(f)
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter,
               fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width() - 8 * s))


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def render(v: Visual, size: int = SIZE) -> QImage:
    s = size / SIZE
    top, bottom, glyph, label, badge_bg, badge_fg = STYLE.get(v.state, STYLE[S_OFF])
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(bottom))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QColor(top))
    grad.setColorAt(1, QColor(bottom))
    p.fillRect(QRectF(0, 0, size, size), QBrush(grad))
    if v.ring:
        pen = QPen(QColor(RING), 7 * s)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(3.5 * s, 3.5 * s, size - 7 * s, size - 7 * s), 12 * s, 12 * s)
    has_label = bool(v.label)
    cy = (66 if has_label else 76) * s
    if v.badge:
        cy += 4 * s
    _glyph(p, v, size / 2, cy, s * (0.9 if has_label else 1.1), QColor(glyph))
    _badge(p, v.badge, s, size, QColor(badge_bg), QColor(badge_fg))
    _fit_label(p, v.label, QRectF(8 * s, 100 * s, size - 16 * s, 40 * s), QColor(label), 24 * s, 13 * s)
    p.end()
    img.setText("ddstate", v.tag)
    img.setText("ddlabel", v.label)
    img.setText("ddbadge", v.badge)
    img.setText("ddring", "1" if v.ring else "0")
    return img


def render_icon(kind: str, size: int, color: str = "#ffffff", background: str | None = None) -> QImage:
    """Action/category/plugin icon: just the symbol (transparent or coloured background)."""
    s = size / SIZE * 1.9
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if background:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(background)))
        r = size * 0.18
        p.drawRoundedRect(QRectF(0, 0, size, size), r, r)
        s = size / SIZE * 1.15
    c = QColor(color)
    if kind == "system":
        _bars(p, size / 2, size / 2 - 2 * s, s, c)
    else:
        _speaker(p, size / 2 + 4 * s, size / 2, s, c, 2, False)
    p.end()
    return img


def to_png(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = img.save(buf, "PNG")
    buf.close()
    if not ok:
        raise RuntimeError("failed to encode PNG")
    return bytes(ba)


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


class Renderer:
    """Cache: the same Visual -> the same image (no redraw)."""

    def __init__(self, size: int = SIZE, limit: int = 256) -> None:
        self.size = size
        self.limit = limit
        self._cache: dict[Visual, str] = {}
        self.renders = 0

    def url(self, v: Visual) -> str:
        u = self._cache.get(v)
        if u is None:
            if len(self._cache) >= self.limit:
                self._cache.clear()
            u = data_url(to_png(render(v, self.size)))
            self._cache[v] = u
            self.renders += 1
        return u
