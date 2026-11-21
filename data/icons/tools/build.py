#!/usr/bin/env python3
"""Generates every SVG of the final Warpipe icon set.

Plain string templates, no dependencies.  Output SVGs are viewBox based,
hand-readable, no external refs, no raster, no text/fonts, no filters.

Run:  python3 tools/build.py [OUTPUT_DIR]     (default: the folder above tools/)
"""
import colorsys
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- palette
OUT = "#0d2a12"          # outline (very dark green)
GREEN = {"base": "#3cb043", "light": "#a4e765", "shade": "#238a2e", "deep": "#145c1c"}
GREY = {"base": "#6a6a6a", "light": "#8c8c8c", "shade": "#555555", "deep": "#3c3c3c"}
WAVE = "#b9f26c"         # lime waves (app icon, system key)
WAVE_HOT = "#e4ffb3"     # brighter waves (key "in stream")
GLOW = "#6dff5e"

LIGHT_FG = "#232629"     # Breeze ColorScheme-Text, light theme
DARK_FG = "#fcfcfc"      # Breeze ColorScheme-Text, dark theme
POSITIVE = "#27ae60"     # Breeze ColorScheme-PositiveText (same in light and dark)

KEY_BG = "#1b1b1b"
RED = "#e5383b"


def dim(pal, s_mul, v_mul):
    """Desaturate/darken a palette (HSV)."""
    res = {}
    for k, hx in pal.items():
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        r, g, b = colorsys.hsv_to_rgb(h, s * s_mul, v * v_mul)
        res[k] = "#%02x%02x%02x" % tuple(round(c * 255) for c in (r, g, b))
    return res


GREEN_DIM = dim(GREEN, 0.6, 0.62)    # key "tool not running"


def f(v):
    """Compact number formatting."""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def arc(cx, cy, r, half_deg, ry=None):
    """Arc (circular, or elliptical with ry) over the top of (cx,cy), left to right."""
    ry = r if ry is None else ry
    a = math.radians(half_deg)
    x1, y1 = cx - r * math.sin(a), cy - ry * math.cos(a)
    x2 = cx + r * math.sin(a)
    return f"M{f(x1)} {f(y1)}A{f(r)} {f(ry)} 0 0 1 {f(x2)} {f(y1)}"


def svg(view, body, size=None):
    w = h = size or view
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" viewBox="0 0 {view} {view}">\n'
            f'{body}</svg>\n')


# ======================================================= 128-grid colour art
# Art spans y 7.5..120.5 (hicolor-style margins), x 17.5..110.5.
SW = 5                                  # outline width
RIM = (20, 60, 88, 24, 6)               # x, y, w, h, rx
BODY = (29, 82, 70, 36, 4)              # starts under the rim (overlap 2)
WAVE_C = (64, 73)                       # wave centre sits inside the rim
WAVES3 = [(24, 40), (41, 36), (58, 33)]  # (radius, half-angle)


def pipe128(p, outline=OUT):
    rx, ry, rw, rh, rr = RIM
    bx, by, bw, bh, br = BODY
    return f"""  <g id="pipe">
    <rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="{br}" fill="{p['base']}"/>
    <rect x="37" y="{by}" width="8" height="{bh}" fill="{p['light']}"/>
    <rect x="49" y="{by}" width="3" height="{bh}" fill="{p['light']}"/>
    <rect x="80" y="{by}" width="12" height="{bh}" fill="{p['shade']}"/>
    <rect x="{bx}" y="{by}" width="{bw}" height="11" fill="{p['deep']}" opacity=".6"/>
    <rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="{br}" fill="none" stroke="{outline}" stroke-width="{SW}" stroke-linejoin="round"/>
    <rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" rx="{rr}" fill="{p['base']}"/>
    <rect x="28" y="{ry}" width="9" height="{rh}" fill="{p['light']}"/>
    <rect x="41" y="{ry}" width="3" height="{rh}" fill="{p['light']}"/>
    <rect x="86" y="{ry}" width="14" height="{rh}" fill="{p['shade']}"/>
    <rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" rx="{rr}" fill="none" stroke="{outline}" stroke-width="{SW}" stroke-linejoin="round"/>
  </g>
"""


def waves128(fill, arcs=WAVES3, inner=8.0, edge=3.5, c=WAVE_C, outline=OUT, flat=None):
    d = " ".join(arc(c[0], c[1], r, a, None if flat is None else r * flat) for r, a in arcs)
    lines = ['  <g id="waves" fill="none" stroke-linecap="round">']
    if edge:
        lines.append(f'    <path d="{d}" stroke="{outline}" stroke-width="{f(inner + 2 * edge)}"/>')
    lines.append(f'    <path d="{d}" stroke="{fill}" stroke-width="{f(inner)}"/>')
    return "\n".join(lines) + "\n  </g>\n"


def app_icon():
    return svg(128, waves128(WAVE) + pipe128(GREEN))


# ------------------------------------------------ small hinted colour icons
def app_icon_16():
    g = GREEN
    d = " ".join([arc(8, 9, 4, 55), arc(8, 9, 7, 34)])
    return svg(16, f"""  <!-- 16px pixel-fitted version: whole-pixel fills, solid waves -->
  <path d="{d}" fill="none" stroke="{g['base']}" stroke-width="2" stroke-linecap="round"/>
  <rect x="3" y="10" width="10" height="6" fill="{OUT}"/>
  <rect x="4" y="11" width="8" height="4" fill="{g['base']}"/>
  <rect x="5" y="11" width="1" height="4" fill="{g['light']}"/>
  <rect x="10" y="11" width="1" height="4" fill="{g['shade']}"/>
  <rect x="1" y="7" width="14" height="4" fill="{OUT}"/>
  <rect x="2" y="8" width="12" height="2" fill="{g['base']}"/>
  <rect x="3" y="8" width="1" height="2" fill="{g['light']}"/>
  <rect x="11" y="8" width="2" height="2" fill="{g['shade']}"/>
""")


def app_icon_22():
    g = GREEN
    d = " ".join([arc(11, 13, 5, 52), arc(11, 13, 9, 36)])
    return svg(22, f"""  <!-- 22px pixel-fitted version: whole-pixel fills, solid waves -->
  <path d="{d}" fill="none" stroke="{g['base']}" stroke-width="2" stroke-linecap="round"/>
  <rect x="4" y="14" width="14" height="7" fill="{OUT}"/>
  <rect x="5" y="15" width="12" height="5" fill="{g['base']}"/>
  <rect x="7" y="15" width="2" height="5" fill="{g['light']}"/>
  <rect x="10" y="15" width="1" height="5" fill="{g['light']}"/>
  <rect x="13" y="15" width="2" height="5" fill="{g['shade']}"/>
  <rect x="5" y="15" width="12" height="1" fill="{g['deep']}" opacity=".6"/>
  <rect x="2" y="10" width="18" height="5" fill="{OUT}"/>
  <rect x="3" y="11" width="16" height="3" fill="{g['base']}"/>
  <rect x="5" y="11" width="2" height="3" fill="{g['light']}"/>
  <rect x="8" y="11" width="1" height="3" fill="{g['light']}"/>
  <rect x="15" y="11" width="2" height="3" fill="{g['shade']}"/>
""")


def app_icon_24():
    """22px art centred on a 24 canvas (1px padding) - stays pixel-crisp."""
    return app_icon_22().replace('viewBox="0 0 22 22"', 'viewBox="-1 -1 24 24"') \
                        .replace('width="22" height="22"', 'width="24" height="24"') \
                        .replace("22px pixel-fitted version", "24px = 22px pixel-fitted art + 1px padding")


def app_icon_32():
    g = GREEN
    d = " ".join([arc(16, 20, 8, 50), arc(16, 20, 14, 36)])
    return svg(32, f"""  <!-- 32px pixel-fitted version: 2px outline, outlined lime waves -->
  <g fill="none" stroke-linecap="round">
    <path d="{d}" stroke="{OUT}" stroke-width="4"/>
    <path d="{d}" stroke="{WAVE}" stroke-width="2"/>
  </g>
  <rect x="6" y="19" width="20" height="11" rx="1" fill="{OUT}"/>
  <rect x="8" y="21" width="16" height="7" fill="{g['base']}"/>
  <rect x="10" y="21" width="2" height="7" fill="{g['light']}"/>
  <rect x="14" y="21" width="1" height="7" fill="{g['light']}"/>
  <rect x="19" y="21" width="3" height="7" fill="{g['shade']}"/>
  <rect x="8" y="21" width="16" height="2" fill="{g['deep']}" opacity=".6"/>
  <rect x="3" y="14" width="26" height="7" rx="1" fill="{OUT}"/>
  <rect x="5" y="16" width="22" height="3" fill="{g['base']}"/>
  <rect x="7" y="16" width="3" height="3" fill="{g['light']}"/>
  <rect x="12" y="16" width="1" height="3" fill="{g['light']}"/>
  <rect x="21" y="16" width="4" height="3" fill="{g['shade']}"/>
""")


# ====================================================== tray / symbolic
# Breeze-like line glyph, drawn with FILLS only (Breeze convention): 1px pipe
# outline on whole pixels, 2px waves whose top rows land on whole pixels.
def rrect(x0, y0, x1, y1, r, ccw=False):
    """Rounded rectangle path (r may be 0)."""
    if r <= 0:
        return rect(x0, y0, x1, y1, ccw)
    if not ccw:
        return (f"M{f(x0 + r)} {f(y0)}H{f(x1 - r)}A{f(r)} {f(r)} 0 0 1 {f(x1)} {f(y0 + r)}"
                f"V{f(y1 - r)}A{f(r)} {f(r)} 0 0 1 {f(x1 - r)} {f(y1)}"
                f"H{f(x0 + r)}A{f(r)} {f(r)} 0 0 1 {f(x0)} {f(y1 - r)}"
                f"V{f(y0 + r)}A{f(r)} {f(r)} 0 0 1 {f(x0 + r)} {f(y0)}Z")
    return (f"M{f(x0 + r)} {f(y0)}A{f(r)} {f(r)} 0 0 0 {f(x0)} {f(y0 + r)}"
            f"V{f(y1 - r)}A{f(r)} {f(r)} 0 0 0 {f(x0 + r)} {f(y1)}"
            f"H{f(x1 - r)}A{f(r)} {f(r)} 0 0 0 {f(x1)} {f(y1 - r)}"
            f"V{f(y0 + r)}A{f(r)} {f(r)} 0 0 0 {f(x1 - r)} {f(y0)}Z")


def rect(x0, y0, x1, y1, ccw=False):
    if ccw:
        return f"M{f(x0)} {f(y0)}V{f(y1)}H{f(x1)}V{f(y0)}Z"
    return f"M{f(x0)} {f(y0)}H{f(x1)}V{f(y1)}H{f(x0)}Z"


def arc_band(cx, cy, r, half_deg, w):
    """Filled outline of a stroked arc (width w, round caps)."""
    a = math.radians(half_deg)
    s, c = math.sin(a), math.cos(a)
    ro, ri, h = r + w / 2, r - w / 2, w / 2
    ol, orr = (cx - ro * s, cy - ro * c), (cx + ro * s, cy - ro * c)
    ir, il = (cx + ri * s, cy - ri * c), (cx - ri * s, cy - ri * c)
    return (f"M{f(ol[0])} {f(ol[1])}A{f(ro)} {f(ro)} 0 0 1 {f(orr[0])} {f(orr[1])}"
            f"A{f(h)} {f(h)} 0 0 1 {f(ir[0])} {f(ir[1])}"
            f"A{f(ri)} {f(ri)} 0 0 0 {f(il[0])} {f(il[1])}"
            f"A{f(h)} {f(h)} 0 0 1 {f(ol[0])} {f(ol[1])}Z")


# rim: outline rect (x0,y0,x1,y1) on half-pixels; body: U under the rim;
# slots: shine gaps used by the filled "active" state; pause: corner badge.
TRAY = {
    22: dict(rim=(3.5, 11.5, 18.5, 15.5), body=(5.5, 15.5, 16.5, 20.5),
             wc=(11, 12), arcs=[(5, 46), (9, 34)],
             slots=[(5, 12, 6, 15), (7, 16, 8, 20)],
             pause=[(18, 1, 19, 6), (20, 1, 21, 6)]),
    16: dict(rim=(1.5, 7.5, 14.5, 10.5), body=(3.5, 10.5, 12.5, 14.5),
             wc=(8, 8), arcs=[(3, 34), (6, 34)],
             slots=[(3, 8, 4, 10), (5, 11, 6, 14)],
             pause=[(13, 0, 14, 4), (15, 0, 16, 4)]),
}


def glyph_paths(size):
    t = TRAY[size]
    x0, y0, x1, y1 = t["rim"]
    # rim ring: outer = rect grown by .5 with r=1, inner = rect shrunk by .5
    ring = rrect(x0 - .5, y0 - .5, x1 + .5, y1 + .5, 1) + rect(x0 + .5, y0 + .5, x1 - .5, y1 - .5, ccw=True)
    bx0, by0, bx1, by1 = t["body"]
    top = y1 + .5        # body legs start below the rim ring
    u = (f"M{f(bx0 - .5)} {f(top)}V{f(by1)}A.5 .5 0 0 0 {f(bx0)} {f(by1 + .5)}"
         f"H{f(bx1)}A.5 .5 0 0 0 {f(bx1 + .5)} {f(by1)}V{f(top)}H{f(bx1 - .5)}"
         f"V{f(by1 - .5)}H{f(bx0 + .5)}V{f(top)}Z")
    pipe = ring + u
    waves = "".join(arc_band(*t["wc"], r, a, 2) for r, a in t["arcs"])
    # silhouette for the filled "active" state, shine slots knocked out
    fill = (f"M{f(x0)} {f(y0)}H{f(x1)}V{f(y1)}H{f(bx1)}V{f(by1)}H{f(bx0)}V{f(y1)}H{f(x0)}Z"
            + "".join(rect(*s, ccw=True) for s in t["slots"]))
    pause = "".join(rect(*p) for p in t["pause"])
    return pipe, waves, fill, pause


def _glyph(size, state, fg, accent, cls_fg="", cls_acc=""):
    pipe, waves, fill, pause = glyph_paths(size)
    out = []
    if state == "active":
        out.append(f'  <path{cls_acc} d="{fill}" fill="{accent}" fill-rule="evenodd"/>')
    out.append(f'  <path{cls_fg} d="{pipe}" fill="{fg}"/>')
    if state == "active":
        out.append(f'  <path{cls_acc} d="{waves}" fill="{accent}"/>')
    elif state == "off":
        out.append(f'  <path{cls_fg} d="{waves}" fill="{fg}" opacity=".35"/>')
        out.append(f'  <path{cls_fg} d="{pause}" fill="{fg}"/>')
    else:
        out.append(f'  <path{cls_fg} d="{waves}" fill="{fg}"/>')
    return "\n".join(out) + "\n"


def tray(size, fg, state):
    """Fixed-colour tray icon. state: idle | active | off"""
    return svg(size, _glyph(size, state, fg, POSITIVE))


def symbolic(size, state):
    """KDE symbolic: Plasma recolours it via the current-color-scheme stylesheet."""
    style = ('  <style type="text/css" id="current-color-scheme">'
             f'.ColorScheme-Text{{color:{LIGHT_FG};}}'
             f'.ColorScheme-PositiveText{{color:{POSITIVE};}}</style>\n')
    return svg(size, style + _glyph(size, state, "currentColor", "currentColor",
                                    ' class="ColorScheme-Text"',
                                    ' class="ColorScheme-PositiveText"'))


# ============================================================ stream deck
# The pipe sits at the same place in every state; its bottom is at ~77% of
# the key height so the title the plugin draws at the bottom stays clear.
KEY_TF = "translate(14.4 2.5) scale(.9)"


def key(state):
    defs, under, art, over = "", "", "", ""
    if state == "absent":
        art = pipe128(GREY, outline="#0b0b0b")
    elif state == "available":
        art = pipe128(GREEN)
    elif state == "instream":
        defs = f"""  <defs>
    <radialGradient id="glow" cx="72" cy="58" r="74" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{GLOW}" stop-opacity=".36"/>
      <stop offset=".6" stop-color="{GLOW}" stop-opacity=".1"/>
      <stop offset="1" stop-color="{GLOW}" stop-opacity="0"/>
    </radialGradient>
  </defs>
"""
        under = (f'  <rect width="144" height="144" fill="url(#glow)"/>\n'
                 f'  <rect x="3.5" y="3.5" width="137" height="137" rx="18" fill="none" '
                 f'stroke="{GLOW}" stroke-width="5"/>\n'
                 f'  <rect x="9" y="9" width="126" height="126" rx="13" fill="none" '
                 f'stroke="{GLOW}" stroke-width="5" opacity=".16"/>\n')
        d = " ".join(arc(WAVE_C[0], WAVE_C[1], r, a) for r, a in WAVES3)
        halo = "".join(f'    <path d="{d}" stroke="{GLOW}" stroke-width="{w}" opacity="{o}"/>\n'
                       for w, o in ((30, ".1"), (20, ".18")))
        art = (f'  <g fill="none" stroke-linecap="round">\n{halo}  </g>\n'
               + waves128(WAVE_HOT, inner=8.5, edge=0) + pipe128(GREEN))
    elif state == "system":
        spray = [(24, 64), (37, 60), (50, 57), (63, 54), (76, 51)]
        art = waves128(WAVE, arcs=spray, inner=6.5, edge=2, flat=.78) + pipe128(GREEN)
    elif state == "notrunning":
        art = pipe128(GREEN_DIM, outline="#0b0b0b")
        over = (f'  <circle cx="113" cy="31" r="24.5" fill="{RED}" stroke="{KEY_BG}" stroke-width="5"/>\n'
                f'  <rect x="109" y="15" width="8" height="21" rx="4" fill="#fff"/>\n'
                f'  <circle cx="113" cy="44" r="4.5" fill="#fff"/>\n')
    body = (defs + f'  <rect width="144" height="144" fill="{KEY_BG}"/>\n' + under
            + f'  <g transform="{KEY_TF}">\n{art}  </g>\n' + over)
    return svg(144, body)


# ============================================================ monochrome
def mono_waves():
    return " ".join(arc(64, 60, r, a) for r, a in [(18, 42), (34, 38), (50, 35)])


def mono():
    """#000 line art for print/README: closed tube, shine strokes, 3 free waves."""
    rx, ry, rw, rh, rr = RIM
    bx, by, bw, bh, br = BODY
    bot = by + bh
    return svg(128, f"""  <g fill="none" stroke="#000" stroke-linecap="round" stroke-linejoin="round">
    <rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" rx="{rr}" stroke-width="{SW}"/>
    <path d="M{bx} {ry + rh}V{bot - 6}A6 6 0 0 0 {bx + 6} {bot}H{bx + bw - 6}A6 6 0 0 0 {bx + bw} {bot - 6}V{ry + rh}" stroke-width="{SW}"/>
    <path d="M31 67V77M39 92V110" stroke-width="4"/>
    <path d="M48 92V110" stroke-width="2.5"/>
    <path d="{mono_waves()}" stroke-width="8"/>
  </g>
""")


def mono_solid():
    """#000 solid silhouette: transparent seam under the rim, shine slots cut out."""
    rim = rrect(17.5, 57.5, 110.5, 85, 8.5)
    body = (f"M26.5 88.5H101.5V113A7.5 7.5 0 0 1 94 120.5H34A7.5 7.5 0 0 1 26.5 113Z")
    slots = rrect(29, 65, 33, 78, 2, ccw=True) + rrect(37, 94, 41, 113, 2, ccw=True) \
        + rrect(46, 94, 48, 113, 1, ccw=True)
    return svg(128, f"""  <path d="{rim}{body}{slots}" fill="#000" fill-rule="evenodd"/>
  <path d="{mono_waves()}" fill="none" stroke="#000" stroke-width="8" stroke-linecap="round"/>
""")


# ================================================================== main
def main():
    out = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    files = {}
    files["warpipe.svg"] = app_icon()
    files["px/16/warpipe.svg"] = app_icon_16()
    files["px/22/warpipe.svg"] = app_icon_22()
    files["px/24/warpipe.svg"] = app_icon_24()
    files["px/32/warpipe.svg"] = app_icon_32()
    for size, sub in ((22, ""), (16, "px/16/")):
        for theme, fg in (("light", LIGHT_FG), ("dark", DARK_FG)):
            for state in ("idle", "active", "off"):
                sfx = "" if state == "idle" else f"-{state}"
                files[f"{sub}warpipe-tray-{theme}{sfx}.svg"] = tray(size, fg, state)
        for state in ("idle", "active", "off"):
            sfx = "" if state == "idle" else f"-{state}"
            files[f"{sub}warpipe{sfx}-symbolic.svg"] = symbolic(size, state)
    for st in ("absent", "available", "instream", "system", "notrunning"):
        files[f"key-{st}.svg"] = key(st)
    files["warpipe-mono.svg"] = mono()
    files["warpipe-mono-solid.svg"] = mono_solid()
    for rel, text in files.items():
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        print(rel)


if __name__ == "__main__":
    main()
