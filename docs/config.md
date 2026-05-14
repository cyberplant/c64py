# Configuration (`c64py.toml`)

c64py supports a single TOML configuration file so you don't have to retype
every CLI flag on every run. CLI flags always override config; config
overrides hardcoded defaults.

## Search order

When you launch `c64py` (or `python C64.py`) without `--config` /
`--no-config`, the loader walks the following list **in order** and stops
at the first existing file. Only that file is parsed — there is no
layering across multiple files.

1. `./c64py.toml` (current working directory — useful for per-game
   settings dropped next to a `.prg`)
2. `~/.c64py.toml`
3. `$XDG_CONFIG_HOME/c64py/c64py.toml`
   (defaults to `~/.config/c64py/c64py.toml` when `XDG_CONFIG_HOME` is
   unset)

If none exist, the built-in defaults (see below) are used.

## Override rules

| Source              | Wins over     |
| ------------------- | ------------- |
| Explicit CLI flag   | config + defaults |
| Config file value   | hardcoded defaults |
| Hardcoded defaults  | (final fallback) |

`--config FILE` forces a specific file and skips the search list.
`--no-config` skips file loading entirely and uses pure defaults
(useful for reproducible test runs).

## Schema

```toml
[video]
rendering  = "per-frame"     # "per-frame" | "per-raster" | "per-cycle" (per-cycle needs accurate-python VIC; see docs/per_cycle_vic.md)
standard   = "pal"           # "pal" | "ntsc"
scale      = 2               # graphics window scale factor (integer)
fps        = 30              # graphics present rate cap
border     = 32              # graphics border size in pixels
fullscreen = false           # hide debug panel / status bar (text mode)

[audio]
emulation = "resid"          # "resid" | "python-sid" | "disabled"
volume    = 1.0              # 0.0 muted .. 1.0 full

[emulation]
interface     = "textual"    # "textual" | "headless" | "graphics"
disk_emulation = "fast"      # "fast" | "accurate-python" | "accurate-rust"
vic_emulation  = "fast"      # "fast" | "accurate-python" | "accurate-rust"

[c1541]
# Standalone TCP drive (`python -m c64py.drives.c1541_emulator`).
# `{date}` → ISO date, `{device}` → drive number (8–11).
file_logging_enabled = false
file_logging_filename = "logs/c1541-{date}.log"

[debug]
turbo     = false            # run at maximum speed (no throttle)
udp_debug = false            # emit debug events over UDP
udp_port  = 64738            # UDP port for debug events
screen_update_interval = 0.1 # seconds between text/status refreshes

[input]
# Reserved for future general-input options (e.g. layout = "us" | "latam").

[input.joystick.port2]
# Host-key bindings for emulated joystick port 2 (the C64 default for most
# games). A direction can be a single key or a list.
# Names are case-insensitive; "Up"/"K_UP", "Space", "LShift",
# individual letters/digits, etc. all resolve to pygame.K_*.
up    = "Up"
down  = "Down"
left  = "Left"
right = "Right"
fire  = ["Space"]

[input.joystick.port1]
# Empty by default. Example WASD remap:
#   up = "W"
#   down = "S"
#   left = "A"
#   right = "D"
#   fire = "LShift"

[input.gamepad]
axis_threshold = 0.5

[input.gamepad.port1]
enabled = false
axis_threshold = 0.5

[input.gamepad.port1.mapping]
up = "axis1-"
down = "axis1+"
left = "axis0-"
right = "axis0+"
fire = "button0"

[input.gamepad.port2]
enabled = false
axis_threshold = 0.5

[input.gamepad.port2.mapping]
up = "axis1-"
down = "axis1+"
left = "axis0-"
right = "axis0+"
fire = "button0"
```

Partial files are deep-merged over the defaults: any key you omit keeps
its built-in value. For example:

```toml
[video]
rendering = "per-raster"
```

is enough to switch a bare `c64py game.prg` to per-raster rendering
without affecting the other knobs.

## Generating a default file

```
c64py --write-config                 # writes ~/.c64py.toml
c64py --write-config ./c64py.toml    # writes one next to your .prg
c64py --write-config /path/file.toml --force-overwrite-config
```

`--write-config` writes a fully-populated TOML with every default
explicit and short comments. It refuses to overwrite an existing file
unless you pass `--force-overwrite-config`.

## CLI flags reference

| Flag                        | Config key             |
| --------------------------- | ---------------------- |
| `--video-rendering`         | `video.rendering`      |
| `--graphics-scale`          | `video.scale`          |
| `--fullscreen`              | `video.fullscreen`     |
| `--audio-emulation`         | `audio.emulation`      |
| `--audio-volume`            | `audio.volume`         |
| `--interface`               | `emulation.interface`  |
| `--vic-emulation`           | `emulation.vic_emulation` |
| `--turbo`                   | `debug.turbo`          |
| `--udp-debug`               | `debug.udp_debug`      |
| `--udp-debug-port`          | `debug.udp_port`       |
| `--screen-update-interval`  | `debug.screen_update_interval` |

Other CLI flags not listed here are run-specific (snapshot paths,
trace files, ROM directory, etc.) and are not currently mirrored in
the config file.

**Drive tier:** `[emulation] disk_emulation` (`fast` \| `accurate-python` \|
`accurate-rust`) controls auto-spawned headless drives and KERNAL shortcut
policy on the C64 host. There is no matching `C64.py` flag; the standalone
1541 uses the same values via `python -m c64py.drives.c1541_emulator --emulation …`.

## Joystick mapping

When graphics mode is active, the host keyboard can drive an emulated
joystick on port 1 and/or port 2. Each direction (`up`, `down`, `left`,
`right`, `fire`) accepts either a single key name or a list of names
(handy for binding multiple fire keys). Bits are active-low at the
CIA1 wires, so multiple bindings simply OR together — the same as
plugging two joysticks into the same port on real hardware.

A host key bound to **both** the keyboard matrix (e.g. `Space`) and a
joystick bit will drive both simultaneously. Real hardware behaves the
same way; games disambiguate via the CIA1 DDR. BASIC ignores joystick
lines, so binding `Space` as fire does not break typing in graphics mode.

If no `[input.joystick]` table is present, the built-in defaults
(arrows + Space for port 2 fire; port 1 empty) apply. To disable a
direction entirely, set it to an empty string or a name pygame can't
resolve (e.g. `up = ""`).

## Gamepad mapping

`[input.gamepad]` configures physical controllers in graphics mode.

**Naming:** `port1` / `port2` name the **emulated C64 joystick connectors** (same
notion as `[input.joystick.port1]` / `port2` — which CIA port you drive).

**Legacy string tokens:** If a mapping value is a plain string (e.g. `up =
"axis0+"`), every such direction on that C64 port reads the **host SDL joystick
at a fixed index**: **port 1 → SDL 0**, **port 2 → SDL 1**. That order can still
change when devices reconnect; use **per-control GUID tables** (below) for
stable binding.

Each C64 port has its **own** `mapping` (and optional per-port `axis_threshold`).

- `axis_threshold` (top-level): default threshold; each `[input.gamepad.portN]`
  may set its own `axis_threshold`.
- `port1` / `port2`: `enabled`, `axis_threshold`, `mapping`.

**Per-control SDL GUID (optional):** Each direction can be a plain string (legacy)
or an inline table so different physical gamepads can drive the **same** emulated
C64 port (e.g. DIY setups). SDL’s joystick **GUID** identifies the *model* (USB
VID/PID + version); it is stable across reboots.

```toml
[input.gamepad.port1.mapping]
# Legacy: all string directions use SDL joystick index 0 on this C64 port
up = "axis1-"
# Explicit device: only this GUID’s control counts for “down”
down = { guid = "050000004c050000e60c000011810000", token = "button1" }
```

Keys: `guid` (or `device_guid` / `sdl_guid`), `token` (or `bind`). If two
identical controllers share a GUID, add `host_index` (SDL joystick index at
capture time) or set it manually (`0` = first enumerated). The interactive editor
records `guid` (+ `host_index` when needed) automatically when you capture a
control.

Supported **token** values (same as before):

- `axisN+` / `axisN-` (e.g. `axis0+`, `axis1-`)
- `hatH:up|down|left|right` (e.g. `hat0:left`)
- `buttonB` (e.g. `button0`)

Older single-block configs (`enabled` + `port` + `mapping` under `[input.gamepad]`)
are migrated automatically on load to the correct `port1` or `port2` table.

## Config editor (`config.py`)

`config.py` hosts config reading/writing and an interactive editor mode.
Pygame is imported only when editor mode is used. If the C64 chargen ROM
(`characters.901225-01.bin`) is found via the same search paths as the emulator,
the editor draws text with an 8×8 C64 font and a blue VIC-like palette; otherwise
it uses a bold monospace system font with the same colors.

```bash
# Open the editor (no flags needed when you run this file as a script):
python path/to/c64py/config.py --config ~/.c64py.toml

# Same module via `-m` prints TOML by default; use `--edit` for the UI:
python -m c64py.config --edit --config ~/.c64py.toml
python -m c64py.config --dump --config ~/.c64py.toml

python -m c64py.config --write-default --config ~/.c64py.toml --force
```

Editor controls:

- `Up/Down`: move (section headers are skipped)
- `Left/Right`: adjust bool / choice / numeric fields
- `Enter`: on **keyboard** joystick rows, capture a host key; on **gamepad** mapping
  rows, capture a button/axis/hat token (opens the capture screen; pygame opens all
  SDL devices first). Bluetooth: focus the window, wake the pad; polling runs ~30s.
  On **`[c1541]`** string fields (e.g. `file_logging_filename`), `Enter` opens a short
  text prompt for the path template (`{date}` / `{device}` placeholders allowed).
- `S`: save
- `Q` or `Esc`: quit; if there are unsaved changes, choose **Save and quit** (default),
  **Discard and quit**, or **Cancel**
