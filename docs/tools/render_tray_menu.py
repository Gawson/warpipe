"""Render the tray menu (fake PipeWire graph, nothing touches real audio) to docs/assets/tray-menu.png.

usage: QT_QPA_PLATFORM=offscreen QT_QPA_PLATFORMTHEME=kde python3 docs/tools/render_tray_menu.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("WARPIPE_LANG", "en")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from helpers import FakePw  # noqa: E402
from scenarios import per_app  # noqa: E402
from warpipe.enforcer import Enforcer  # noqa: E402
from warpipe.state import Config, SnapshotStore  # noqa: E402
from warpipe.tray import TrayController  # noqa: E402

app = QApplication(sys.argv[:1])
with tempfile.TemporaryDirectory() as d:
    b, _ns = per_app()
    enf = Enforcer(FakePw(b), Config(Path(d) / "config.json"), SnapshotStore(Path(d) / "snap.json"))
    enf.refresh()
    ctl = TrayController(enf)
    ctl.app_actions["Warframe.x64.exe"].trigger()
    app.processEvents()
    menu = ctl.menu
    menu.ensurePolished()
    menu.adjustSize()
    pm = menu.grab()
    icon = ctl.tray.icon().pixmap(44, 44) if hasattr(ctl, "tray") else QPixmap()

pad = 16
canvas = QPixmap(pm.width() + 2 * pad, pm.height() + 2 * pad + 56)
canvas.fill(Qt.transparent)
p = QPainter(canvas)
p.setRenderHint(QPainter.Antialiasing)
# fake panel strip with the tray icon
panel = QRectF(pad, canvas.height() - 52, pm.width(), 44)
pp = QPainterPath(); pp.addRoundedRect(panel, 8, 8)
p.fillPath(pp, QColor("#202326"))
if not icon.isNull():
    p.drawPixmap(int(panel.right() - 56), int(panel.top()), icon)
# menu with soft shadow
mp = QPainterPath(); mp.addRoundedRect(QRectF(pad, pad, pm.width(), pm.height()), 6, 6)
p.fillPath(mp.translated(0, 3), QColor(0, 0, 0, 70))
p.setClipPath(mp); p.drawPixmap(pad, pad, pm); p.setClipping(False)
p.setPen(QPen(QColor(0, 0, 0, 90), 1)); p.drawPath(mp)
p.end()
out = ROOT / "docs" / "assets" / "tray-menu.png"
canvas.save(str(out))
print(out, canvas.width(), "x", canvas.height())
