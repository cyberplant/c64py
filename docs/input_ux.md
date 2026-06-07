# Input / UX additions

This document tracks runtime host-side UX controls and input expansion work.

## Runtime hotkeys

### Graphics mode (`--graphics`)

- `F10`: toggle turbo mode.
- `F11`: toggle windowed/fullscreen display.
- `F12`: save a PNG screenshot to `snapshots/`.

### Textual UI

- `F10`: toggle turbo mode.
- `F11`: toggle Textual fullscreen layout mode.
- `F12`: save a text-screen snapshot (`.txt`) to `snapshots/`.

## Gamepad support

Graphics mode can map a physical gamepad to C64 joystick lines via
`[input.gamepad]` and `[input.gamepad.mapping]` in `c64py.toml`.

You must set `enabled = true` under `[input.gamepad.port1]` and/or
`[input.gamepad.port2]` for each port you want driven from a physical
controller (the emulator logs this on startup).

For troubleshooting, run with:

```bash
C64PY_DEBUG_GAMEPAD=1 python C64.py --graphics …
```

That prints every `JOY*` event (axis, button, hat) to the console so you can
see SDL button indices and fix your `buttonN` / `axisN±` mappings.

See `docs/config.md` for the complete schema and mapping token format.
