"""Wtyczka OpenDeck / Stream Deck „Warpipe”.

  logic  — czysta logika (bez Qt): ustawienia, stan narzędzia → wygląd przycisku,
           reakcja na naciśnięcie, wyszukanie programu warpipe
  render — rysowanie przycisków (QImage/QPainter → PNG → data URL)
  main   — proces wtyczki: WebSocket do OpenDeck + gniazdo sterowania narzędzia

Wtyczka nigdy sama nie zmienia połączeń PipeWire. Prosi o to działające narzędzie
(ikona w zasobniku albo --watch) przez $XDG_RUNTIME_DIR/warpipe/control.sock.
"""

__version__ = "1.0.0"
