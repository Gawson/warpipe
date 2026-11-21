#!/usr/bin/python3 -I
"""Rysuje statyczne ikony wtyczki (manifest) tym samym kodem, którym wtyczka rysuje przyciski.

  icons/plugin(.png|@2x.png)         72/144  — ikona wtyczki
  icons/category(.png|@2x.png)       28/56   — ikona kategorii „Discord”
  icons/action-app(.png|@2x.png)     20/40   — ikona akcji na liście
  icons/action-system(.png|@2x.png)  20/40
  icons/key-app(.png|@2x.png)        72/144  — domyślny obrazek przycisku (zanim wtyczka go zmieni)
  icons/key-system(.png|@2x.png)     72/144

Użycie: python3 make_icons.py  (QT_QPA_PLATFORM nie trzeba ustawiać — używa offscreen)
"""

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
PLUGIN = os.path.join(HERE, "io.github.gawson.warpipe.sdPlugin")
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(PLUGIN, "lib"))

from PySide6.QtGui import QGuiApplication  # noqa: E402


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(["make_icons", "-platform", "offscreen"])  # noqa: F841
    from warpipe_plugin.logic import S_OFF, S_SYS_OFF, SYSTEM_LABEL, Visual
    from warpipe_plugin.render import render, render_icon

    out = os.path.join(PLUGIN, "icons")
    os.makedirs(out, exist_ok=True)

    def save(img, name):
        path = os.path.join(out, name)
        if not img.save(path, "PNG"):
            raise SystemExit(f"nie udało się zapisać {path}")

    for scale, suffix in ((1, ""), (2, "@2x")):
        save(render_icon("app", 72 * scale, "#ffffff", "#22a356"), f"plugin{suffix}.png")
        save(render_icon("app", 28 * scale, "#ffffff"), f"category{suffix}.png")
        save(render_icon("app", 20 * scale, "#ffffff"), f"action-app{suffix}.png")
        save(render_icon("system", 20 * scale, "#ffffff"), f"action-system{suffix}.png")
        save(render(Visual("app", S_OFF, "", ""), 72 * scale), f"key-app{suffix}.png")
        save(render(Visual("system", S_SYS_OFF, SYSTEM_LABEL, ""), 72 * scale), f"key-system{suffix}.png")
    print(f"ikony zapisane w {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
