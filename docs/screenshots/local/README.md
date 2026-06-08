# Local screenshot media (not in git)

Use this directory for **your own** PRG/D64 files (commercial games, demos, etc.). They are **not** committed: global `.gitignore` excludes `*.prg` and `*.d64`.

## Setup

1. Copy the example manifest:
   ```bash
   cp docs/screenshots/local/manifest.example.json docs/screenshots/local/manifest.local.json
   ```
2. Drop media files next to `manifest.local.json`.
3. Edit `manifest.local.json` (cycles, outputs, optional disk attach).
4. Run:
   ```bash
   pip install -r requirements-docs.txt
   C64PY_ROM_DIR=./roms python scripts/capture_local_screenshots.py
   ```

PNG outputs default to `docs/screenshots/local/output/` (also gitignored).

To run only specific entries, pass `--id` (comma-separated and/or repeated):

```bash
python scripts/capture_local_screenshots.py --id my_demo,movie_script
python scripts/capture_local_screenshots.py --id movie_script --dry-run
```

Pass `--show` to open a live pygame window that mirrors captured frames while it
runs (useful when iterating locally; omit it for headless/CI). `--show-scale N`
sets the window zoom (default 2):

```bash
python scripts/capture_local_screenshots.py --id my_demo --show --show-scale 3
```

Each entry prints its effective config before running, e.g.:

```
[my_demo] config: vic_emulation=accurate-python video=PAL clock=985248Hz turbo=on | render interface=graphics mode=latched border=32 | audio=off display=off
```

Use this to confirm your manifest values are actually applied.

## Manifest fields

| Field | Meaning |
|-------|---------|
| `id` | Label for logs |
| `media` | Filename in this directory (`.prg` or `.d64`) |
| `output` | PNG path relative to this directory |
| `skip_if_missing` | If `true`, skip when `media` is absent (default `true`) |
| `boot_cycles` | Cycles after ROM load before loading media |
| `after_run_cycles` | Cycles to run after load/RUN before capture |
| `auto_run` | Inject `RUN` after PRG load (default `true`) |
| `attach_drive` | Device number when `media` is `.d64` |
| `load_prg` | File on disk to load when using `.d64` |
| `vic_emulation` | VIC-II **timing** engine: `fast`, `accurate-python`, `accurate-rust` (alias `accurate`→`accurate-python`, `rust`→`accurate-rust`) |
| `render.mode` | Frame **compositing**: `latched` (per-frame), `beam` (per-raster line), or `per-cycle` (per-cycle, matches the live `per-cycle` path). Aliases: `per-frame`/`fast`→`latched`, `per-raster`/`accurate`→`beam`, `percycle`→`per-cycle` |
| `render.interface` | `graphics` or `textual` (Textual chrome PNG) |
| `keys` | Keypresses to inject during the run (see below) |
| `frames[]` | Optional extra PNGs at relative cycle offsets from capture state |
| `script[]` | Timeline of timed key/screenshot/video events (supersedes the above; see below) |

See `manifest.example.json` for samples.

### Accuracy: `vic_emulation` vs `render.mode`

These are two independent knobs and **both** matter for clean output:

- `vic_emulation` controls VIC-II **timing** (when registers take effect). Only
  `fast`, `accurate-python`, `accurate-rust` are valid (`accurate` is accepted as
  an alias for `accurate-python`). An invalid value is reported clearly per entry.
- `render.mode` controls how a frame is **composited** into the PNG/video. From
  coarsest to finest:
  - `latched` — samples the VIC once per frame (cheap; default). Demos that change
    VIC registers mid-frame (raster splits, color bars, split HUD/playfield) look
    **glitchy** here even with accurate timing.
  - `beam` — reconstructs the frame from one VIC snapshot **per raster line**, so
    full-line raster splits render correctly. Effects that change registers *in the
    middle of a line* can still leave a garbled scanline band.
  - `per-cycle` — samples the VIC on a per-cycle (per character-column) grid. This
    is the **same path the live emulator uses with `--video-rendering per-cycle`**,
    so what you capture matches what you see while playing. Use this when `beam`
    shows a "horizontal line that comes and goes". Both `beam` and `per-cycle` need
    an accurate timing engine (`accurate-python` or `accurate-rust`); with `fast`
    the per-line/per-cycle buffers aren't populated and the frame falls back to
    `latched`.

So if your video looks torn/glitchy, set `render.mode` to `"per-cycle"` (or at
least `"beam"`) **and** `vic_emulation` to `"accurate-rust"` (or
`"accurate-python"`). `per-cycle` matches the live experience most closely. The
per-entry config line printed at startup shows exactly what's in use.

### Injecting keypresses (`keys`)

Some demos wait for a keypress (e.g. SPACE) before starting. The `keys` field
injects keystrokes during the `after_run_cycles` window:

```json
"keys": [
  { "cycles": 8000000, "press": " " },
  { "cycles": 12000000, "press": "{F1}" }
]
```

- `cycles` is an **offset relative to media-load completion**, i.e. the start of
  the `after_run_cycles` window (not the absolute `current_cycles`). `0` fires
  right after the PRG loads / `RUN` is injected.
- A key whose offset exceeds `after_run_cycles` still fires; the run extends to
  reach it before capturing.
- `press` uses the same syntax as the `--inject-keys` CLI flag: a single space
  is SPACE, lowercase letters map to PETSCII uppercase, `\n` / `\r` is RETURN,
  and `{F1}`–`{F8}` are the function keys. Joystick tokens are **not** supported
  in capture mode (keyboard only).
- A bare string (`"keys": " "`) is shorthand for one press at offset `0`.

Because keys fire during the main run, any `frames[]` captures (which step from
the post-run snapshot) already reflect the keys that were pressed.

### Multi-frame cycle offsets

`frames[].cycles` is **relative to the post-run capture state** (after boot +
load + `after_run_cycles`), not an absolute cycle count. `cycles: 0` is the same
instant as the main capture; one PAL frame is `312 * 63 = 19656` cycles.

## Timeline / "movie script" mode (`script`)

For richer captures — pressing keys, taking several screenshots, and recording
**video with SID audio** — give an entry a `script`: a list of timed events.
When `script` is present it **supersedes** `after_run_cycles`, `keys`, `frames`,
and the single `output`; the timeline fully drives the run after media load.

```json
{
  "id": "demo",
  "media": "demo.prg",
  "auto_run": true,
  "script": [
    { "at": "0c",  "screenshot": "output/title.png" },
    { "at": "2s",  "key": " " },
    { "at": "3s",  "video": "20s:output/demo.mp4" },
    { "at": "5s",  "key": "{F1}" },
    { "at": "30s", "video": { "duration": "8s", "output": "output/end.mp4", "fps": 50, "audio": true } }
  ]
}
```

### Timestamps (`at`)

Every event has an `at` (alias `cycle`) measured as an **offset from media-load
completion** (the same origin as `after_run_cycles`). Units:

| Suffix | Meaning | Example |
|--------|---------|---------|
| `c` | emulator cycles (default if bare number) | `"19656c"`, `200000` |
| `s` | seconds (× PAL master clock 985248 Hz) | `"2.5s"` |
| `ms` | milliseconds | `"500ms"` |
| `f` | PAL frames (`19656` cycles each) | `"50f"` |

Events are sorted by time, so you can list them in any order.

### Event actions (exactly one per event)

| Action | Form | Effect |
|--------|------|--------|
| `key` (alias `keys`) | string | Inject keypresses (`" "`=SPACE, `{F1}`–`{F8}`, letters, `\n`=RETURN) |
| `joystick` (alias `joy`) | string or object | Hold a joystick direction/button |
| `screenshot` | PNG path | Capture a still at that instant |
| `video` | string or object | Record a clip starting at `at` (see below) |

### Keys: buffer vs. matrix (`method`)

```json
{ "at": "2s", "key": " " }                                  // buffer (default)
{ "at": "2s", "key": " ", "method": "matrix", "hold": "80ms" }
{ "at": "2s", "key": "{f1}", "method": "matrix" }
```

- **`method: "buffer"`** (default): stuffs the KERNAL keyboard buffer. Instant and
  reliable for software that reads keys via the KERNAL (BASIC `GET`/`INPUT`,
  `GETIN`). Does **not** reach games that scan the keyboard hardware directly.
- **`method: "matrix"`**: emulates a real key press in the CIA1 keyboard matrix
  (press → hold → release). Works for both KERNAL-based input *and* games that
  poll `$DC00`/`$DC01` themselves. Options: `hold` (default `80ms`) is how long
  each key is held; `gap` (default `40ms`) is the pause between characters when a
  string types several keys serially.
- Matrix tokens: literal characters, `\n`/`\r` (RETURN), and `{space}`,
  `{f1}`–`{f8}`, `{up}`/`{down}`/`{left}`/`{right}`, `{home}`, `{del}`,
  `{runstop}`, `{ctrl}`, `{cbm}`, `{shift}`. Unsupported characters are skipped
  with a warning (shifted symbols like `!`/`"` aren't mapped yet).

### Joystick events

```json
{ "at": "3s", "joy": "2:fire:300ms" }                       // port 2, fire, 300ms
{ "at": "3s", "joy": "2:up+fire" }                          // default hold 250ms
{ "at": "5s", "joystick": { "port": 1, "press": "left", "hold": "500ms" } }
```

- String form `"<port>:<dirs>[:<hold>]"`; object form `{ port, press|dir, hold }`.
- `port` is `1` or `2` (most games use port 2).
- Directions/buttons: `up`, `down`, `left`, `right`, `fire` (alias `button`),
  combined with `+` (e.g. `"up+fire"`).
- The line is held low for `hold` (default `250ms`) then released, exactly like a
  real stick — handy for "press fire to start" screens.

### Video events

A `video` action records a clip. Two forms:

- **String** `"<duration>:<output>"` — e.g. `"20s:output/demo.mp4"` records 20s
  starting at the event time, 50 fps, with audio.
- **Object** `{ "duration"|"to", "output", "fps", "audio" }` — `duration` is
  relative to `at`; `to` is an absolute timeline timestamp; `fps` defaults to
  `50`; `audio` defaults to `true`.

Notes:

- Video requires **ffmpeg** on `PATH` (`brew install ffmpeg`) and only works with
  `render.interface: "graphics"` (not `textual`).
- **SID audio** is captured deterministically: an offline reSID instance is
  attached and clocked in lockstep with the CPU, so audio and frames stay in
  sync regardless of how fast the capture runs. This needs the reSID library
  (`resid_c.dylib`/`.so`) built — see `src/resid_wrapper/README.md`. Without it,
  video is still produced but silent.
- Frames are piped straight into ffmpeg (no PNG sequence on disk); the SID PCM is
  muxed in as AAC afterward.
- Video outputs are skipped by the PNG size-budget check.
- One recording window at a time; overlapping `video` windows are ignored with a
  warning.

## Video

Video (with SID audio) is built in via [timeline mode](#timeline--movie-script-mode-script):
add a `video` event to an entry's `script`. Frames are piped directly into
`ffmpeg` and the SID PCM is muxed as AAC, so no intermediate PNG sequence is
written. Requires `ffmpeg` on `PATH`.

If you instead want a raw PNG sequence (e.g. to encode to WebM/GIF yourself),
`frames[]` still works:

```bash
ffmpeg -framerate 50 -i output/effect_f%01d.png -c:v libvpx-vp9 -pix_fmt yuv420p effect.webm
```
