"""Render the Warpipe window (fake PipeWire graph, nothing touches real audio) to docs/assets/popup.png:
dark and light Breeze palettes side by side. Nothing is shown on screen.

usage: QT_QPA_PLATFORM=offscreen QT_QPA_PLATFORMTHEME=kde python3 docs/tools/render_popup.py [OUT.png]
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("WARPIPE_LANG", "en")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleFactory  # noqa: E402

from helpers import FakePw  # noqa: E402
from scenarios import per_app  # noqa: E402
from warpipe.enforcer import Enforcer  # noqa: E402
from warpipe.icons import DesktopIndex  # noqa: E402
from warpipe.popup import PopupWindow, popup_model  # noqa: E402
from warpipe.state import Config, SnapshotStore  # noqa: E402

BREEZE = {
    "dark": dict(Window="#202326", WindowText="#fcfcfc", Base="#141618", AlternateBase="#1d1f22",
                 Text="#fcfcfc", Button="#292c30", ButtonText="#fcfcfc", Mid="#4d5257",
                 Midlight="#33363a", Light="#3a3e42", Dark="#141618", Highlight="#3daee9",
                 HighlightedText="#fcfcfc", ToolTipBase="#292c30", ToolTipText="#fcfcfc",
                 disabled="#7f8388", icons="breeze-dark"),
    "light": dict(Window="#eff0f1", WindowText="#232629", Base="#fcfcfc", AlternateBase="#f7f7f7",
                  Text="#232629", Button="#fcfcfc", ButtonText="#232629", Mid="#b8bcc0",
                  Midlight="#dcdfe2", Light="#ffffff", Dark="#b8bcc0", Highlight="#3daee9",
                  HighlightedText="#fcfcfc", ToolTipBase="#f7f7f7", ToolTipText="#232629",
                  disabled="#8a8f94", icons="breeze"),
}


def palette(name: str) -> QPalette:
    spec = BREEZE[name]
    p = QPalette()
    for role, color in spec.items():
        if role in ("disabled", "icons"):
            continue
        p.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(spec["disabled"]))
    return p


def framed(pm: QPixmap, margin: int = 12) -> QPixmap:
    canvas = QPixmap(pm.width() + 2 * margin, pm.height() + 2 * margin)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(margin, margin, pm.width(), pm.height()), 10, 10)
    p.fillPath(path.translated(0, 3), QColor(0, 0, 0, 60))
    p.setClipPath(path)
    p.drawPixmap(margin, margin, pm)
    p.setClipping(False)
    p.setPen(QPen(QColor(0, 0, 0, 70), 1))
    p.drawPath(path)
    p.end()
    return canvas


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "assets" / "popup.png"
    app = QApplication(sys.argv[:1])
    if "Breeze" in QStyleFactory.keys():
        app.setStyle("Breeze")
    shots = []
    with tempfile.TemporaryDirectory() as d:
        b, _ns = per_app()
        enf = Enforcer(FakePw(b), Config(Path(d) / "config.json"), SnapshotStore(Path(d) / "snap.json"),
                       enabled=True, selected=["Spotify", "Warframe.x64.exe"], persist=False)
        st = enf.refresh()
        for name in ("dark", "light"):
            QIcon.setThemeName(BREEZE[name]["icons"])
            app.setPalette(palette(name))
            w = PopupWindow()
            w.set_model(popup_model(st, DesktopIndex([])))   # theme icons only: same picture everywhere
            w.ensurePolished()
            w.resize(w.width(), w.height())
            shots.append(framed(w.grab()))
            w.deleteLater()
    gap = 0
    canvas = QPixmap(sum(s.width() for s in shots) + gap, max(s.height() for s in shots))
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    x = 0
    for s in shots:
        p.drawPixmap(x, 0, s)
        x += s.width() + gap
    p.end()
    canvas.save(str(out))
    print(out, canvas.width(), "x", canvas.height())


if __name__ == "__main__":
    main()
