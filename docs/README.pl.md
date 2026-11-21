> Wersja robocza (nazwa robocza: „Dźwięk do Discorda” / `discord-dzwiek`). Docelowo projekt nazywa się **Warpipe**.

# Dźwięk do Discorda (`discord-dzwiek`)

Małe narzędzie z ikoną w zasobniku. Sprawia, że do **dźwięku streamu w Discordzie
(Go Live)** trafiają **tylko aplikacje, które zaznaczysz**, np. sama gra bez YouTube'a
z Firefoksa.

Dlaczego to potrzebne: oficjalny Discord na Linuksie (Wayland) udostępnia albo dźwięk
całego systemu, albo żaden. Portal ekranu (xdg-desktop-portal) nie potrafi przekazać
dźwięku pojedynczej aplikacji. Discord ogłosił, że pracuje nad tym z Igalią, ale nie podał
terminu.

## Jak używać

1. Kliknij ikonę głośnika w zasobniku (obok zegara) **prawym przyciskiem**.
2. Pod „Aplikacje z dźwiękiem” zaznacz grę, np. `Warframe.x64.exe`.
   Zaznaczenie od razu włącza „Tylko zaznaczone do Discorda”.
3. W Discordzie zacznij stream (Go Live) **z włączonym dźwiękiem**. Narzędzie samo wykryje
   stream i przełączy dźwięk. Wyskoczy krótkie powiadomienie.

Wybór zostaje zapamiętany. Przy następnym streamie nie trzeba nic klikać.

- **Cały system (przywróć)** wraca do zwykłego zachowania Discorda: idzie cały dźwięk.
- **Zakończ** zamyka narzędzie i też przywraca zwykłe zachowanie.
- Pierwsza linijka menu pokazuje stan: `Stream z dźwiękiem: wykryty` albo `nie wykryty`.
- Najedź myszką na aplikację na liście, żeby zobaczyć, co aktualnie gra (np. tytuł karty).
  Ta podpowiedź widnieje tylko w menu i nigdzie się nie zapisuje.

## Instalacja

Potrzebne pakiety (na Fedorze zwykle już są): `python3`, `python3-pyside6` i
`pipewire-utils` (dostarcza `pw-dump` i `pw-link`). Nic nie trzeba instalować przez pip.

```bash
# z katalogu z tym README:
mkdir -p ~/.local/share/discord-dzwiek ~/.local/bin ~/.local/share/applications
cp -r discord_dzwiek discord-dzwiek ~/.local/share/discord-dzwiek/
ln -sf ~/.local/share/discord-dzwiek/discord-dzwiek ~/.local/bin/discord-dzwiek
cp discord-dzwiek.desktop ~/.local/share/applications/
```

Potem „Dźwięk do Discorda” pojawi się w menu programów. Można też wpisać `discord-dzwiek`
w terminalu.

### Autostart

```bash
mkdir -p ~/.config/autostart
cp discord-dzwiek-autostart.desktop ~/.config/autostart/
```

Można też przez Ustawienia systemowe, Autostart, „Dodaj…”, i wybrać „Dźwięk do Discorda”.
Przy starcie sesji narzędzie czeka do minuty, aż pojawi się panel Plasmy.

## Odinstalowanie

Najpierw zamknij narzędzie przez menu „Zakończ”. Ewentualnie uruchom
`discord-dzwiek --restore`. Potem usuń pliki:

```bash
rm -rf ~/.local/share/discord-dzwiek ~/.config/discord-dzwiek
rm -f ~/.local/bin/discord-dzwiek ~/.local/share/applications/discord-dzwiek.desktop \
      ~/.config/autostart/discord-dzwiek-autostart.desktop
```

Narzędzie nie zmienia żadnych plików systemowych ani konfiguracji
PipeWire/WirePlumbera. Nie trzeba niczego więcej sprzątać.

## Jak to działa

Oficjalny Discord (sprawdzone w module `discord_voice` 1.0.137 i w zgłoszeniach
użytkowników) nie nagrywa „całego systemu” jednym strumieniem. Dla **każdej grającej
aplikacji** otwiera przez pipewire-pulse osobny strumień nagrywania:

- nazwa: `discord_capture`, a media.name to `game capture`;
- `target.object` wskazuje na tę aplikację;
- WirePlumber łączy wyjścia aplikacji bezpośrednio z tym strumieniem.

Discord pomija aplikacje, które nie podają swojego PID (`application.process.id`).
Przykład to Minecraft przez OpenAL na natywnym PipeWire.

Narzędzie pilnuje, żeby do tych strumieni Discorda wchodziły **wyłącznie** wyjścia
wybranych aplikacji:

- strumienie Discorda dla aplikacji niewybranych zostają odłączone (dostają ciszę);
- wybrana aplikacja bez własnego strumienia Discorda (np. Minecraft) zostaje dołączona
  do jednego z istniejących strumieni Discorda. PipeWire sam miksuje kilka połączeń
  na jednym wejściu;
- każda wybrana aplikacja trafia do Discorda dokładnie raz, z dopasowanymi kanałami
  (FL→FL, FR→FR; mono → oba kanały);
- jeśli jakiś klient czyta zamiast tego **monitor wyjścia** (np. `easyeffects_sink`
  albo karta ALSA), połączenia z monitora zostają zastąpione połączeniami z wybranych
  aplikacji. Taki strumień wygląda jednak tak samo jak **mikrofon ustawiony na monitor**
  (wirtualny mikrofon, soundboard), więc za pierwszym razem narzędzie go nie rusza:
  menu pokazuje „niepewny” i podmenu „Który strumień Discorda?” (w wierszu poleceń
  `--capture ID`). Po jednorazowym wyborze potwierdzenie (program, nazwa strumienia,
  wyjście) zostaje zapisane w konfiguracji i potem działa samo.

Pozostałe zasady:

- **Mikrofon rozmowy** (`WEBRTC VoiceEngine` / `recStream`) nigdy nie jest ruszany.
  Własny dźwięk Discorda (`playStream`) nie pojawia się na liście.
- Przed pierwszą zmianą narzędzie zapisuje **migawkę** połączeń
  (`$XDG_RUNTIME_DIR/discord-dzwiek/snapshot.json`). „Cały system” i wyjście z programu
  odtwarzają usunięte połączenia i kasują połączenia dodane przez narzędzie.
  Dodane połączenia są oznaczone właściwością `discord-dzwiek.managed`.
  Gdy ponowne zaznaczenie aplikacji odtwarza jej **oryginalne** połączenie, dostaje ono
  znacznik `discord-dzwiek.restored` zamiast `managed`, bo to znowu stan sprzed zmian.
  „Cały system” go zostawia.
- Migawka jest związana z bieżącym uruchomieniem PipeWire (cookie i PID demona).
  Numery obiektów PipeWire po jego restarcie zaczynają się od nowa, więc migawka
  z poprzedniego uruchomienia jest w całości odrzucana. Połączenie z migawki jest
  odtwarzane tylko wtedy, gdy zgadzają się numery **i** nazwy węzłów oraz portów, a wejście
  należy do strumienia nagrywania Discorda.
- Po każdym utworzeniu połączenia narzędzie sprawdza, czy trafiło ono tam, gdzie miało.
  PipeWire od razu używa ponownie numerów zamkniętych portów. Jeśli połączenie trafiło
  do innego portu, narzędzie je usuwa (tylko połączenie z własnym znacznikiem).
- Narzędzie zmienia **tylko połączenia, które kończą się w strumieniu nagrywania
  Discorda**. Nie dotyka urządzeń, wyjść, wejść ani innych programów. Nie potrzebuje
  sudo i nie edytuje konfiguracji WirePlumbera.
- Pilnowanie działa w tle. `pw-dump --monitor` służy tylko jako sygnał „coś się
  zmieniło”. Po ~0,25 s spokoju narzędzie robi jeden pełny `pw-dump` i poprawia
  połączenia, jeśli trzeba. W bezczynności nie zużywa CPU.
  Dodatkowo co 15 s robi kontrolny odczyt. Jeśli monitor padnie, odczytuje stan co 2 s.
  Jeśli coś ciągle przywraca połączenia, narzędzie na chwilę przestaje pilnować i
  pokazuje ostrzeżenie.

Ustawienia są w `~/.config/discord-dzwiek/config.json`:

```json
{
  "version": 1,
  "selected_apps": ["Warframe.x64.exe"],
  "enforce": true,
  "capture_match_terms": ["discord", "vesktop", "vencord", "venmic", "webrtc"],
  "capture_names": ["discord_capture", "game capture"],
  "mic_names": ["WEBRTC VoiceEngine", "recStream"]
}
```

Trzy ostatnie klucze są opcjonalne i mają takie wartości domyślne. Służą do dopasowania,
gdyby Discord kiedyś zmienił nazwy swoich strumieni. Zawężenie `capture_match_terms`
zmienia tylko wykrywanie strumienia nagrywania. Własne strumienie Discorda (np. głosy
z rozmowy, `playStream`) i Vesktopa nigdy nie pojawiają się na liście aplikacji.

Po potwierdzeniu strumienia „z monitora” (patrz wyżej) w pliku pojawia się też klucz
`confirmed_captures`, np. `[{"program": "Discord", "node": "Discord", "sink":
"easyeffects_sink"}]`. Usunięcie wpisu cofa potwierdzenie.

`"enforce"` musi być prawdziwym `true`/`false` z JSON-a. Tekst `"false"` też znaczy
„wyłączone”. Jeśli pliku nie da się odczytać (np. złe kodowanie), narzędzie wypisuje
ostrzeżenie i używa ustawień domyślnych.

Nazwa aplikacji to `application.name`.
Jeśli go brak, narzędzie bierze nazwę programu, a potem `node.name`. Dla gier z Protona
`application.name` to zwykle nazwa pliku `.exe`, dla Minecrafta `java`.

## Wiersz poleceń

```
discord-dzwiek                     ikona w zasobniku (domyślnie w sesji graficznej)
discord-dzwiek --list [--json]     co widać: strumienie Discorda (i dlaczego) + aplikacje
discord-dzwiek --apply APP... [--capture ID] [--dry-run]
                                   jednorazowo: tylko te aplikacje (bez pilnowania)
discord-dzwiek --restore [--dry-run]
                                   przywróć stan sprzed zmian
discord-dzwiek --watch [--apply APP...]
                                   pilnowanie bez ikony; Ctrl+C / SIGTERM przywraca
discord-dzwiek --dump PLIK         zrzut diagnostyczny bez prywatnych danych
discord-dzwiek -v ...              więcej komunikatów
```

Kody wyjścia: 0 ok, 1 błąd PipeWire, 2 złe argumenty, 3 brak albo niepewny strumień
Discorda, 4 działa już inna instancja (ikona albo `--watch`). Naraz może działać tylko
jedna instancja zmieniająca połączenia.

`--apply` z nazwą, której teraz nie ma (np. literówka), niczego nie zmienia: kończy się
kodem 2 i wypisuje nazwy aplikacji, które grają. Jeśli pasuje tylko część nazw,
narzędzie wykonuje zmianę i ostrzega, które aplikacje **nie** pójdą do Discorda.
`--capture ID` przy strumieniu „z monitora” zapisuje też potwierdzenie (jak wybór w menu).

## Ograniczenia

- Narzędzie **filtruje** to, co Discord nagrywa. Nie umie zmusić Discorda do
  nagrywania. Jeśli w Discordzie nie ma ani jednego strumienia `discord_capture`, menu
  pokazuje „nie wykryty” i nie ma czego przełączać. Tak jest np. przy streamie bez
  dźwięku albo gdy gra tylko Minecraft, który nie podaje PID.
  Minecraft da się dołączyć tylko wtedy, gdy Discord nagrywa coś jeszcze, np. grę z PID.
- Gdy zaczyna grać nowa, **niewybrana** aplikacja (np. dźwięk powiadomienia), Discord
  od razu otwiera dla niej strumień, a WirePlumber go podłącza. Narzędzie odłącza go po
  ok. 0,3 s, ale ułamek sekundy może przejść do streamu.
- Wykrywanie opiera się na tym, jak działa Discord na dzień 2026-11. Jeśli Discord to
  zmieni, menu pokaże „nie wykryty” albo „niepewny”. Wtedy przyda się `--list` / `--dump`.
  W razie niepewności narzędzie **nie zgaduje**: w menu pojawia się „Który strumień
  Discorda?”, a w wierszu poleceń trzeba podać `--capture ID`.
- WirePlumber potrafi zapamiętać **wyciszenie** strumieni `discord_capture`. Wtedy
  każdy nowy stream startuje wyciszony. Narzędzie tylko ostrzega i niczego nie odcisza.
- Jeśli strumień Discorda straci swoją aplikację, WirePlumber może podłączyć do niego
  **mikrofon**. Narzędzie to pokazuje. Przy włączonym pilnowaniu takie połączenie
  zostaje usunięte.
- `--apply` działa jednorazowo: nowe strumienie Discorda nie zostaną poprawione.
  Do stałego działania służy ikona albo `--watch`.
- Po zabiciu procesu przez `kill -9` połączenia zostają, aż Discord zamknie stream.
  Można je cofnąć przez `discord-dzwiek --restore`. Po restarcie PipeWire nie ma czego
  cofać: wszystkie połączenia i tak znikają, a stara migawka jest odrzucana.
- Vesktop ma własny wybór aplikacji (venmic) i lepiej użyć tamtego. To narzędzie
  rozpozna strumień Vesktopa czytający z `vencord-screen-share`, ale venmic może
  „walczyć” o połączenia.
- Narzędzie testowano na żywo na Fedorze 45 (PipeWire 1.6.11, WirePlumber 0.5.19)
  na **atrapach** strumieni Discorda. Na docelowym komputerze warto raz sprawdzić
  `discord-dzwiek --list` w trakcie Go Live z dźwiękiem. Powinno być
  `Stream z dźwiękiem: wykryty` i węzły `discord_capture`.

## Rozwiązywanie problemów

1. `discord-dzwiek --list` pokazuje, które strumienie Discorda widać i dlaczego zostały
   (albo nie zostały) uznane za dźwięk streamu, oraz jakie aplikacje grają.
2. `discord-dzwiek --dump zrzut.json` zapisuje stan PipeWire bez tytułów kart i utworów
   (`media.name` → `<media>`) i bez danych osobistych (nazwa użytkownika i komputera,
   PID-y). Ten plik można komuś wysłać.
3. Jeśli coś zostało w dziwnym stanie, `discord-dzwiek --restore` przywraca połączenia.
4. Logi ikony z autostartu: `journalctl --user -b | grep discord-dzwiek`.
   Ręcznie: `discord-dzwiek -v`.

## Testy (dla opiekuna)

```bash
python3 -m unittest discover -s tests -v   # testy jednostkowe (stdlib unittest)
bash tests/live_pipewire.sh                  # test na żywo: własny null sink, cisza, sprzątanie
```

Test na żywo tworzy tylko własne obiekty: null sink `dd-test-sink`, atrapy aplikacji
grające ciszę z `/dev/zero` i atrapy strumieni Discorda nagrywające do `/dev/null`.
Narzędzie dostaje w nim konfigurację, która widzi wyłącznie węzły `dd-test-discord*`.
Na koniec test sprawdza w `pw-dump`, że po teście nic nie zostało i że stan
WirePlumbera się nie zmienił.

## Pliki

- `discord-dzwiek`: uruchamiacz (python3, bez venv)
- `discord_dzwiek/graph.py`: model grafu z `pw-dump`
- `discord_dzwiek/planner.py`: czysta logika (wykrywanie, lista aplikacji, plan połączeń,
  przywracanie)
- `discord_dzwiek/pw.py`: wywołania `pw-dump` / `pw-link`
- `discord_dzwiek/state.py`: konfiguracja, migawka, blokada instancji
- `discord_dzwiek/enforcer.py`: pilnowanie (stan, monitor grafu, debounce)
- `discord_dzwiek/cli.py`: wiersz poleceń; `discord_dzwiek/tray.py`: ikona (PySide6)
- `discord_dzwiek/scrub.py`: zrzut diagnostyczny bez prywatnych danych
- `discord-dzwiek.desktop`, `discord-dzwiek-autostart.desktop`: wpisy menu i autostartu
- `tests/`: testy jednostkowe, zanonimizowane prawdziwe zrzuty z KDE/Wayland, `live_pipewire.sh`
