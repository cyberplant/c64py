# Capturing screenshots & videos

How to (re)generate the images in the README and capture your own
screenshots/videos from games and demos. This is developer/contributor
tooling — end users don't need any of it.

## Regenerate README assets

Requires `petcat` (VICE tools), pygame, and the docs extras:

```bash
pip install -r requirements-docs.txt
C64PY_ROM_DIR=./roms python scripts/capture_readme_screenshots.py
```

Outputs land in `docs/images/readme/` with a size budget (target ≤10 KB, warn
>10 KB, fail >20 KB per image). Fixtures live in
`test/fixtures/readme_screenshots/`.

| Scenario | Output |
|----------|--------|
| BASIC READY (pygame) | `boot_ready_graphics.png` |
| Textual UI chrome + screen | `textual_ui.png` |

## Capture your own games/demos

Drop your own PRG/D64 files under `docs/screenshots/local/` (binaries there are
gitignored), copy `manifest.example.json` to `manifest.local.json`, edit it, then
run:

```bash
C64PY_ROM_DIR=./roms python scripts/capture_local_screenshots.py
```

The manifest supports a "movie script" timeline: timed key/joystick injection,
screenshots, and cycle-accurate video recording with real SID audio (piped to
`ffmpeg`). Use `vic_emulation = accurate-rust` + `render.mode = per-cycle` for
output that matches the live emulator. See
[docs/screenshots/local/README.md](screenshots/local/README.md) for the full
manifest schema, the timeline grammar, and the `--id` / `--show` flags.
