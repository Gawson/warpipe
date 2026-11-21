#!/usr/bin/env python3
"""Renders the PNGs of the final Warpipe set with QtSvg (the renderer Plasma
uses) and builds contact-sheet.png.

Run:  python3 tools/render.py [ICON_DIR]     (default: the folder above tools/)
Writes: warpipe-{16,22,32,48,64,128,256}.png, key-*-{144,72}.png, contact-sheet.png
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else \
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_PNG = {16: "px/16/warpipe.svg", 22: "px/22/warpipe.svg", 32: "px/32/warpipe.svg",
           48: "warpipe.svg", 64: "warpipe.svg", 128: "warpipe.svg", 256: "warpipe.svg"}
KEYS = ["absent", "available", "instream", "system", "notrunning"]

LIGHT_BG, DARK_BG = QColor("#eff0f1"), QColor("#2a2e32")      # Breeze window colours
PANEL_L, PANEL_D = QColor("#ffffff"), QColor("#1f2225")
INK_L, INK_D = QColor("#232629"), QColor("#e8e9ea")

_cache = {}


def load(rel, recolor=None):
    key = (rel, recolor)
    if key not in _cache:
        with open(os.path.join(ROOT, rel)) as fh:
            data = fh.read()
        if recolor:                      # what Plasma does with current-color-scheme
            data = data.replace("#232629", recolor)
        r = QSvgRenderer(QByteArray(data.encode()))
        if not r.isValid():
            raise SystemExit(f"invalid SVG: {rel}")
        _cache[key] = r
    return _cache[key]


def render(rel, size, recolor=None):
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    load(rel, recolor).render(p, QRectF(0, 0, size, size))
    p.end()
    return img


def write_pngs():
    n = 0
    for s, rel in APP_PNG.items():
        render(rel, s).save(os.path.join(ROOT, f"warpipe-{s}.png"))
        n += 1
    for k in KEYS:
        for s in (144, 72):
            render(f"key-{k}.svg", s).save(os.path.join(ROOT, f"key-{k}-{s}.png"))
            n += 1
    return n


# ----------------------------------------------------------- contact sheet
def zoom(img, k):
    return img.scaled(img.width() * k, img.height() * k, Qt.IgnoreAspectRatio, Qt.FastTransformation)


def label(p, x, y, text, dark, size=11, bold=False):
    font = QFont("Noto Sans")
    font.setPixelSize(size)
    font.setBold(bold)
    p.setFont(font)
    p.setPen(INK_D if dark else INK_L)
    p.drawText(x, y, text)


class Sheet:
    W, COL = 2000, 1000            # left half light, right half dark

    def __init__(self):
        self.rows = []

    def add(self, h, fn):
        self.rows.append((h, fn))

    def build(self, path):
        H = sum(h for h, _ in self.rows) + 30
        img = QImage(self.W, H, QImage.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        p.fillRect(0, 0, self.COL, H, LIGHT_BG)
        p.fillRect(self.COL, 0, self.COL, H, DARK_BG)
        y = 10
        for h, fn in self.rows:
            for dark in (False, True):
                fn(p, self.COL * dark, y, dark)
            y += h
        label(p, 16, H - 10, "All renders: QtSvg (= Plasma). Icons contain no text; labels belong to this sheet only.",
              False, 11)
        p.end()
        img.save(path)
        return img.width(), img.height()


def row(items, title, h, zoom_k=0, zoom_max=24):
    """items: (rel | fn(dark)->(rel, recolor), size, tag)"""
    def fn(p, x0, y0, dark):
        label(p, x0 + 16, y0 + 18, title, dark, 13, True)
        x, base_y = x0 + 16, y0 + 30
        for rel, size, tag in items:
            recolor = None
            if callable(rel):
                rel, recolor = rel(dark)
            img = render(rel, size, recolor)
            if rel.startswith("warpipe-mono") and dark:      # print art: black on a white card
                p.fillRect(x - 4, base_y - 4, size + 8, size + 8, QColor("#ffffff"))
            p.drawImage(x, base_y, img)
            label(p, x, base_y + max(size, 16) + 14, tag, dark, 10)
            x += max(size, 52) + 12
            if zoom_k and size <= zoom_max:
                z = zoom(img, zoom_k)
                p.fillRect(x - 2, base_y - 2, z.width() + 4, z.height() + 4, PANEL_D if dark else PANEL_L)
                p.drawImage(x, base_y, z)
                label(p, x, base_y + z.height() + 14, f"{tag} x{zoom_k}", dark, 10)
                x += z.width() + 22
    return h, fn


def by_theme(name, sub=""):
    def pick(dark):
        return f"{sub}{name.format(t='dark' if dark else 'light')}", None
    return pick


def recolored(rel):
    def pick(dark):
        return rel, ("#fcfcfc" if dark else None)
    return pick


def build_sheet(path):
    sh = Sheet()
    sh.add(*row([(rel, s, f"{s}px" + (" (px/)" if rel.startswith("px/") else "")) for s, rel in APP_PNG.items()],
                "1. App icon: warpipe-<size>.png as installed (16/22/32 from px/<size>/warpipe.svg, 48+ from warpipe.svg)", 320))
    sh.add(*row([("warpipe.svg", s, f"{s}") for s in (16, 22, 24, 32)]
                + [("px/24/warpipe.svg", 24, "px/24")],
                "1b. Scalable warpipe.svg at small sizes (why px/ exists), and px/24", 150, zoom_k=4, zoom_max=24))

    for sfx, nm in (("", "idle"), ("-active", "filtering active"), ("-off", "off / paused")):
        sh.add(*row([(by_theme(f"warpipe-tray-{{t}}{sfx}.svg"), 22, "22"),
                     (by_theme(f"warpipe-tray-{{t}}{sfx}.svg"), 44, "44 HiDPI"),
                     (by_theme(f"warpipe-tray-{{t}}{sfx}.svg", "px/16/"), 16, "px/16")],
                    f"2. Tray {nm}: left warpipe-tray-light{sfx}.svg, right warpipe-tray-dark{sfx}.svg", 185, zoom_k=6))

    for sub, sz in (("", 22), ("px/16/", 16)):
        sh.add(*row([(recolored(f"{sub}warpipe{s}-symbolic.svg"), sz, f"{n} {sz}")
                     for s, n in (("", "idle"), ("-active", "active"), ("-off", "off"))],
                    f"3. {sub}warpipe[-active|-off]-symbolic.svg (currentColor; right half recoloured like Breeze Dark)",
                    185 if sz == 22 else 150, zoom_k=6))

    sh.add(*row([(f"key-{k}.svg", 144, k) for k in KEYS], "4. Stream Deck key-*.svg @144", 200))
    sh.add(*row([(f"key-{k}.svg", 72, k) for k in KEYS], "4b. Stream Deck @72 (Elgato original / V2)", 130))
    sh.add(*row([("warpipe-mono.svg", s, f"{s}px") for s in (32, 64, 128, 256)],
                "5. warpipe-mono.svg (#000 outline, print/README; white card on dark half)", 315))
    sh.add(*row([("warpipe-mono-solid.svg", s, f"{s}px") for s in (32, 64, 128, 256)],
                "5b. warpipe-mono-solid.svg (#000 silhouette, stickers/stamps)", 315))
    return sh.build(path)


def main():
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    n = write_pngs()
    wh = build_sheet(os.path.join(ROOT, "contact-sheet.png"))
    print(f"{n} PNGs, contact sheet {wh[0]}x{wh[1]}")


if __name__ == "__main__":
    main()
