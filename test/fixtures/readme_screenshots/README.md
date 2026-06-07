# README screenshot fixtures

Deterministic inputs for `scripts/capture_readme_screenshots.py`.

| File | Purpose |
|------|---------|
| `boot_ready.snap` | Post–BASIC-boot state (~5M cycles) |
| `graphics_demo_static.bas` / `.prg` | Bitmap border box + sprite (non-interactive) |
| `graphics_demo_static.snap` | Saved state after running the demo |
| `drive_listing.bas` / `.prg` | Prints directory after `LOAD"$",8` |
| `drive_listing.snap` | Saved state with listing on screen |
| `hello_disk.bas` / `.prg` | One-line program written onto the demo D64 |

Regenerate snapshots and PNGs:

```bash
pip install -r requirements-docs.txt
C64PY_ROM_DIR=./roms python scripts/capture_readme_screenshots.py
```

Remove `*.snap` to force fixture snapshots to be rebuilt on the next run.
