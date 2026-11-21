<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.gawson.Warpipe.svg" width="128" alt="Warpipe icon: a green pipe with sound waves">
</p>

<h1 align="center">Warpipe</h1>

<p align="center"><b>Per-app PipeWire audio for Discord Go Live on Linux.</b><br>
Stream your game — not your YouTube tab, music or notifications.</p>

---

## Why

On Linux (Wayland) the official Discord client can only share **all desktop audio or none** when you
stream with Go Live. Share a game and your viewers also hear everything else that plays.
Discord is working on per-app audio sharing via xdg-desktop-portal, but it is not there yet.

Warpipe fills the gap today: pick the apps your viewers should hear, everything else stays private.

## How it works

<p align="center"><img src="docs/assets/how-it-works.svg" width="820" alt="Diagram: apps go to effects and headphones as usual; Discord opens one capture stream per app; Warpipe keeps only the chosen apps linked to Discord"></p>

The official Discord client captures Go Live audio through `pipewire-pulse`: it opens one record
stream per playing application (`discord_capture`, media name `game capture`), and WirePlumber links
each app's output to it. Warpipe rewires exactly those links:

- only the apps you pick stay linked to Discord's capture streams — you still hear everything;
- the choice is remembered and applied automatically whenever Go Live with audio starts;
- apps Discord never captures on its own (native PipeWire apps without a PID, e.g. Minecraft/OpenAL)
  can be routed into the stream too;
- audio is taken **before** your effects (e.g. EasyEffects), just like Discord does it;
- the original links are snapshotted and restored on exit or with **Whole system**;
- microphones, devices, sinks and WirePlumber configuration are never touched.

## Tray

Click the green pipe in the system tray, tick the apps that should be heard on stream, and turn on
**Only selected apps**. **Whole system** gives you Discord's normal behaviour back.

<!-- screenshot of the tray menu: docs/assets/tray-menu.png (added with the English UI) -->

A popup window with live per-app status is planned for v0.2:

<p align="center"><img src="docs/assets/popup-preview-v0.2.png" width="760" alt="Mockup of the planned v0.2 popup window, dark and light theme"></p>

## Stream Deck / OpenDeck

Warpipe ships a plugin for [OpenDeck](https://github.com/nekename/OpenDeck) (Elgato Stream Deck on Linux):

<p align="center"><img src="docs/assets/streamdeck-keys.png" width="640" alt="Key states: app not playing, available, on stream, whole system, Warpipe not running"></p>

- **App audio** key — configure it with an app (e.g. your game). Grey while the app is not playing,
  green when available, glowing when it is on stream. Press to add/remove it.
- **Whole system** key — back to Discord's default (everything is shared).

The plugin talks to the running Warpipe over a local socket; it never rewires audio by itself.

## Install

Requirements: Linux with PipeWire + WirePlumber, Python 3, PySide6 (`python3-pyside6` on Fedora),
`pw-dump` / `pw-link`, and the official Discord desktop client (Flatpak or native).

```sh
git clone https://github.com/Gawson/warpipe.git
cd warpipe
./install.sh              # user-level, into ~/.local (no root)
./install.sh --autostart  # …and start Warpipe at login
```

Then start **Warpipe** from your application menu, or run `warpipe`.
Uninstall with `./uninstall.sh` (add `--purge` to also remove settings). A running Warpipe restores
Discord's original links when it exits.

OpenDeck plugin: run `opendeck-plugin/build.sh`, then in OpenDeck open **Plugins → Install from file**
and pick `opendeck-plugin/dist/io.github.gawson.warpipe.streamDeckPlugin`.

## Command line

| Command | What it does |
|---|---|
| `warpipe` | tray icon (default in a graphical session) |
| `warpipe --list [--json]` | Discord capture streams and apps that play audio |
| `warpipe --apply APP…` | one-shot: only these apps go to the stream |
| `warpipe --watch` | keep enforcing without a tray icon (Ctrl+C restores) |
| `warpipe --restore` | restore the original links (whole system) |
| `warpipe --status` / `--toggle APP` / `--system` | talk to the running instance |
| `warpipe --dry-run …` | show what would change |
| `warpipe --dump FILE` | anonymized `pw-dump` for bug reports (no titles, user or host names) |

Settings live in `~/.config/warpipe/config.json` (selected apps, enforcement on start, language:
`auto`, `en` or `pl`).

## Limitations

- Linux + PipeWire + the **official** Discord client. Vesktop users already have a per-app picker (venmic).
- When a new, unselected app starts playing, WirePlumber links it immediately; Warpipe cuts it within
  a fraction of a second, so a very short blip can reach the stream.
- PID-less apps (e.g. Minecraft) can only join the stream while Discord has at least one capture
  stream open (i.e. some other app with a PID is playing).
- Tested on Fedora 45, KDE Plasma 6 (Wayland), PipeWire 1.6, WirePlumber 0.5.

## Development

```sh
python3 -m unittest discover -s tests   # unit tests, no PipeWire needed
bash tests/live_pipewire.sh             # live test: creates its own null sink and silent
bash tests/live_plugin.sh               #   test streams, never touches your devices, cleans up
```

The unit tests include two anonymized `pw-dump` snapshots of a real KDE/Wayland desktop
(Go Live without and with audio). Icons are original vector drawings (`data/icons/tools/`).

## Not affiliated

Warpipe is an independent project. It is not affiliated with or endorsed by Discord, the PipeWire
project, Elgato or Nintendo.

## License

[MIT](LICENSE) © 2026 Tomasz Gawlik
