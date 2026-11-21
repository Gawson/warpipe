"""Warpipe — pick which apps are heard in your Discord Go Live stream.

Modules:
  graph    — PipeWire graph model built from pw-dump output (no side effects)
  planner  — pure logic: Discord capture detection, app list, link plan (apply)
             and restore plan
  pw       — adapter calling pw-dump / pw-link
  state    — config, link snapshot, single-instance lock
  enforcer — the loop that keeps the links right (pw-dump --monitor + debounce)
  control  — control channel of the running instance (Unix socket, JSON lines)
  i18n     — tiny translation helper (English UI, optional Polish)
  cli      — command-line interface
  tray     — system tray icon (PySide6): left click = window, right click = menu
  popup    — the window (status, per-app switches), model + widgets
  icons    — the green pipe icon, its fallbacks, app icons for the window
"""

__version__ = "0.1.0"
APP_NAME = "Warpipe"
APP_ID = "warpipe"
DESKTOP_ID = "io.github.gawson.Warpipe"  # .desktop file / icon theme name
