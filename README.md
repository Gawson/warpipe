# Warpipe

**Per-app PipeWire audio for Discord Go Live on Linux.**

> ⚠️ Work in progress. The code in this first commit still uses the working name
> `discord-dzwiek` and Polish UI strings; the rename to `warpipe`, an English UI
> (with optional Polish translation), the new popup window and the OpenDeck /
> Stream Deck plugin are coming in the next commits.

## The problem

On Linux (Wayland), the official Discord client can only share **all desktop audio or
none** when you stream with Go Live. Share a game, and your viewers also hear YouTube,
music, notifications — everything that plays.

Discord is working on per-app audio sharing (via xdg-desktop-portal), but it is not
there yet.

## What Warpipe does

The official Discord client captures stream audio through `pipewire-pulse`: it opens one
record stream per playing application (`discord_capture`, media name `game capture`) and
WirePlumber links each app's output to it.

Warpipe rewires those links so that **only the apps you pick** reach the stream:

- pick apps in a tray menu (or via CLI); the choice is remembered and re-applied
  automatically when Go Live starts;
- unpicked apps are disconnected from Discord's capture streams (you still hear them);
- apps Discord would never capture (e.g. native PipeWire apps without a PID such as
  Minecraft/OpenAL) can be routed into an existing capture stream;
- audio is taken **before** effects (e.g. EasyEffects), exactly like Discord does;
- original links are snapshotted and restored on exit ("Whole system");
- it never touches microphones, devices, sinks or WirePlumber configuration.

## Requirements

- Linux with PipeWire + WirePlumber (tested on Fedora 45, PipeWire 1.6.11, WirePlumber 0.5.19)
- Python 3, PySide6 (for the tray), `pw-dump` and `pw-link`
- the official Discord desktop client (Flatpak or native)

## Quick start (current WIP)

```sh
./discord-dzwiek --list            # Discord capture streams + apps that play audio
./discord-dzwiek --apply Firefox   # one-shot: only Firefox goes to the stream
./discord-dzwiek --watch           # keep enforcing (Ctrl+C restores)
./discord-dzwiek --restore         # back to "whole system"
./discord-dzwiek                   # tray icon
```

## Tests

```sh
python3 -m unittest discover -s tests      # unit tests (no PipeWire needed)
bash tests/live_pipewire.sh                # live test on your own PipeWire: creates its own
                                           # null sink and silent test streams, cleans up
```

The unit tests include two anonymized `pw-dump` snapshots from a real KDE/Wayland desktop
(Go Live without and with audio).

## Not affiliated

Warpipe is an independent project. It is not affiliated with or endorsed by Discord,
the PipeWire project or Nintendo.

## License

MIT — see [LICENSE](LICENSE).
