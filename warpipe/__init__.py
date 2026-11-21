"""Warpipe — wybór aplikacji, których dźwięk trafia do streamu Discorda.

Moduły:
  graph    — model grafu PipeWire zbudowany z wyjścia pw-dump (bez efektów ubocznych)
  planner  — czysta logika: wykrywanie przechwytywania Discorda, lista aplikacji,
             plan połączeń (apply) i przywracania (restore)
  pw       — adapter wywołujący pw-dump / pw-link
  state    — konfiguracja, migawka połączeń, blokada pojedynczej instancji
  enforcer — pętla pilnująca połączeń (pw-dump --monitor + debounce)
  control  — kanał sterowania działającą instancją (gniazdo Unix, JSON w liniach)
  cli      — interfejs wiersza poleceń
  tray     — ikona w zasobniku (PySide6)
"""

__version__ = "1.1.0"
APP_NAME = "Warpipe"
APP_ID = "warpipe"
