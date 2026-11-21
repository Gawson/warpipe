"""Warpipe OpenDeck / Stream Deck plugin.

  logic  — pure logic (no Qt): settings, tool state -> key appearance, key
           presses, finding the warpipe program, language (English / Polish)
  render — key drawing (QImage/QPainter -> PNG -> data URL)
  main   — plugin process: WebSocket to OpenDeck + the tool's control socket

The plugin never changes PipeWire links itself. It asks the running tool
(tray icon or --watch) to do so via $XDG_RUNTIME_DIR/warpipe/control.sock.
"""

__version__ = "1.0.0"
