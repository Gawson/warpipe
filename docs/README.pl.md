# Warpipe (po polsku)

**Warpipe** pozwala wybrać, które aplikacje słychać w streamie Discord Go Live na Linuksie (PipeWire).
Oficjalny Discord pod Waylandem udostępnia cały dźwięk systemu albo żaden; Warpipe zostawia
w strumieniach przechwytujących Discorda tylko wybrane aplikacje. Ty dalej słyszysz wszystko.

- instalacja: `./install.sh` (do `~/.local`, bez roota), usunięcie: `./uninstall.sh`;
- ikona w zasobniku — lewy klik: okno, prawy klik: menu; włącz aplikacje, które mają być słyszalne
  na streamie (**Tylko zaznaczone do Discorda**), **Cały system** przywraca zwykłe zachowanie Discorda;
- Discord na Linuksie wysyła na stream własne dźwięki powiadomień; Warpipe domyślnie ich nie
  wpuszcza, także w trybie **Cały system** (`warpipe --discord-sounds on` je wpuszcza);
- interfejs jest domyślnie po angielsku; po polsku przy `LANG=pl_PL…` albo `"language": "pl"`
  w `~/.config/warpipe/config.json` (lub `WARPIPE_LANG=pl`);
- opcjonalna wtyczka do OpenDecka (Stream Deck): przycisk na aplikację i „Cały system”;
- Go Live gaśnie po powrocie do gry na pełnym ekranie? Udostępniaj cały ekran, nie okno gry: wiele
  gier przy Alt+Tab z powrotem stawia okno od nowa i Discord traci udostępniane okno. Widzowie
  zobaczą cały ekran, więc Discorda trzymaj na drugim monitorze.

Pełna dokumentacja (po angielsku): [README.md](../README.md).
