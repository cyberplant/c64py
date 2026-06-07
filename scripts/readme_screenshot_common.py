"""Shared helpers for reproducible README screenshot capture."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from capture_timeline import (
    PAL_CLOCK_HZ,
    NTSC_CLOCK_HZ,
    VideoRecorder,
    ffmpeg_available,
    parse_matrix_keys,
    parse_script,
)

PAL_CYCLES_PER_FRAME = 312 * 63

# Image budget guardrails (bytes).
TARGET_MAX_BYTES = 10_000
FAIL_MAX_BYTES = 20_000

# C64 text screen geometry (matches emulator constants).
SCREEN_COLS = 40
SCREEN_ROWS = 25
BORDER_WIDTH = 4
BORDER_HEIGHT = 3


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bootstrap() -> str:
    root = repo_root()
    parent = str(root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return root.name


_PKG = bootstrap()
_C64 = import_module(f"{_PKG}.emulator").C64
_load_snapshot = import_module(f"{_PKG}.snapshot").load_snapshot
_save_snapshot = import_module(f"{_PKG}.snapshot").save_snapshot
_expand_inject_payload = import_module(f"{_PKG}.keyboard_inject").expand_inject_payload

# Live-display options. When enabled, the captured frames are also blitted to a
# real pygame window so you can watch the emulator while it captures (handy for
# local debugging; leave off for headless/CI runs).
_DISPLAY = {"show": False, "scale": 2}


def set_display_options(*, show: bool = False, scale: int = 2) -> None:
    """Toggle the visible pygame window for subsequent captures."""
    _DISPLAY["show"] = bool(show)
    _DISPLAY["scale"] = max(1, int(scale))


def show_enabled() -> bool:
    return bool(_DISPLAY["show"])


# Canonical VIC timing engines + friendly aliases accepted in the manifest.
_VIC_CHOICES = ("fast", "accurate-python", "accurate-rust")
_VIC_ALIASES = {
    "accurate": "accurate-python",
    "accurate-py": "accurate-python",
    "accurate_python": "accurate-python",
    "python": "accurate-python",
    "rust": "accurate-rust",
    "accurate_rust": "accurate-rust",
}


def _normalize_vic(value: Any) -> Tuple[str, Optional[str]]:
    """Map a manifest ``vic_emulation`` value to a canonical engine name.

    Returns ``(canonical, note)`` where ``note`` is a human message when an alias
    was applied (or ``None``). Raises ``ValueError`` on unknown values.
    """
    v = str(value).strip().lower()
    if v in _VIC_CHOICES:
        return v, None
    if v in _VIC_ALIASES:
        mapped = _VIC_ALIASES[v]
        return mapped, f"vic_emulation {value!r} -> {mapped!r}"
    raise ValueError(
        f"invalid vic_emulation {value!r}; choose one of {list(_VIC_CHOICES)} "
        f"(aliases: 'accurate'->accurate-python, 'rust'->accurate-rust)"
    )


# Manifest render.mode -> canonical compositing engine. Aliases mirror the live
# emulator's --video-rendering names so the manifest can use whichever the user
# is familiar with from playing interactively.
_RENDER_MODE_CHOICES = ("latched", "beam", "per-cycle")
_RENDER_MODE_ALIASES = {
    "per-frame": "latched",
    "perframe": "latched",
    "fast": "latched",
    "per-raster": "beam",
    "perraster": "beam",
    "raster": "beam",
    "accurate": "beam",
    "percycle": "per-cycle",
    "per_cycle": "per-cycle",
    "cycle": "per-cycle",
}


def _normalize_render_mode(value: Any) -> str:
    """Map a manifest ``render.mode`` value to a canonical compositing engine.

    Canonical values: ``latched`` (per-frame), ``beam`` (per-raster line),
    ``per-cycle`` (per-cycle VIC sampling, matches the live ``per-cycle`` path).
    Unknown values fall back to ``latched``.
    """
    v = str(value).strip().lower()
    if v in _RENDER_MODE_CHOICES:
        return v
    return _RENDER_MODE_ALIASES.get(v, "latched")


def _present_frame(pygame, display, rgb: bytes, width: int, height: int, scale: int) -> None:
    """Blit a captured RGB frame to a visible window and pump events."""
    surf = pygame.image.frombuffer(rgb, (width, height), "RGB")
    if scale != 1:
        surf = pygame.transform.scale(surf, (width * scale, height * scale))
    display.blit(surf, (0, 0))
    pygame.display.flip()
    for _ev in pygame.event.get():
        pass


@dataclass
class SizeReport:
    path: Path
    size: int
    ok: bool
    message: str


def validate_image_budget(path: Path) -> SizeReport:
    size = path.stat().st_size
    if size > FAIL_MAX_BYTES:
        return SizeReport(
            path, size, False,
            f"FAIL: {path.name} is {size} bytes (> {FAIL_MAX_BYTES} budget)",
        )
    if size > TARGET_MAX_BYTES:
        return SizeReport(
            path, size, True,
            f"WARN: {path.name} is {size} bytes (> {TARGET_MAX_BYTES} target)",
        )
    return SizeReport(path, size, True, f"OK: {path.name} is {size} bytes")


def print_size_reports(reports: Iterable[SizeReport]) -> int:
    exit_code = 0
    for rep in reports:
        prefix = "WARN" if rep.ok and rep.size > TARGET_MAX_BYTES else ("FAIL" if not rep.ok else "OK")
        print(f"[{prefix}] {rep.message}")
        if not rep.ok:
            exit_code = 1
    return exit_code


def resolve_rom_dir(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("C64PY_ROM_DIR")
    if env:
        return Path(env).expanduser().resolve()
    for candidate in (
        repo_root() / "roms",
        Path.home() / "roms",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    raise RuntimeError(
        "ROM directory not found. Set C64PY_ROM_DIR or pass --rom-dir."
    )


def make_emu(
    *,
    vic_emulation: str = "fast",
    turbo: bool = True,
) -> object:
    if not _DISPLAY["show"]:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    emu = _C64(vic_emulation=vic_emulation, interface_factory=lambda _e: None)
    emu.turbo = turbo
    return emu


def load_roms(emu: object, rom_dir: Path, *, require_char: bool = False) -> None:
    emu.load_roms(str(rom_dir), require_char_rom=require_char)


def run_to_cycles(emu: object, target: int) -> None:
    """Advance emulator to *target* cumulative cycles (IEC-aware when possible)."""
    stuck = 0
    while int(emu.current_cycles) < target:
        before = int(emu.current_cycles)
        try:
            cyc = emu.run_cpu_instruction_quantum(before, target)
        except Exception as exc:
            print(f"cpu step failed: {exc}", file=sys.stderr)
            return
        if cyc <= 0:
            stuck += 1
            if stuck > 200:
                print("cpu stuck — halting step loop", file=sys.stderr)
                return
            continue
        stuck = 0
        emu.current_cycles = before + cyc
        if hasattr(emu, "sync_keyboard_host_queue"):
            emu.sync_keyboard_host_queue()
        if hasattr(emu, "_step_iec_drives"):
            emu._step_iec_drives(cyc)


def save_snap(emu: object, path: Path, *, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_snapshot(emu, path, note=note)


def refresh_text_screen(emu: object) -> None:
    if hasattr(emu, "_update_text_screen"):
        emu._update_text_screen()


def load_snap(emu: object, path: Path) -> None:
    _load_snapshot(emu, path)
    refresh_text_screen(emu)


def c64_palette_rgb() -> Sequence[Tuple[int, int, int]]:
    return _C64._C64_PALETTE_RGB


def _save_rgb_png(rgb_bytes: bytes, width: int, height: int, path: Path) -> None:
    """Save an RGB buffer as an optimized PNG."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for README screenshots. "
            "Install with: pip install -r requirements-docs.txt"
        ) from exc

    img = Image.frombytes("RGB", (width, height), rgb_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)


def render_pygame_frame(
    emu: object,
    path: Path,
    *,
    cycles: int = 0,
    mode: str = "latched",
    border_size: int = 32,
) -> Tuple[int, int]:
    """Render one pygame frame from emulator state; returns (width, height)."""
    if cycles > 0:
        run_to_cycles(emu, cycles)

    pygame = __import__("pygame")
    if not pygame.get_init():
        pygame.init()

    graphics_mod = import_module(f"{_PKG}.graphics")
    ui = graphics_mod.PygameInterface(emu, scale=1, fps=30, border_size=border_size)
    ui._pygame = pygame
    ui._display_surface = pygame.display.set_mode((1, 1))
    ui._setup_surfaces()
    native_w, native_h = ui._native_size
    if _DISPLAY["show"]:
        scale = int(_DISPLAY["scale"])
        display_surface = pygame.display.set_mode((native_w * scale, native_h * scale))
        ui._display_surface = display_surface
        pygame.display.set_caption("c64py capture")

    mode = _normalize_render_mode(mode)
    if mode == "per-cycle":
        emu.memory.per_cycle_render_enabled = True
        emu.memory.ensure_per_cycle_buffers()
        if not getattr(emu.memory, "per_cycle_snapshots_primed", False):
            emu.memory.prime_per_cycle_snapshots_from_current_vic()
        ui._render_frame_per_cycle()
    elif mode == "beam":
        emu.memory.beam_render_enabled = True
        emu.memory.ensure_beam_buffers()
        if not getattr(emu.memory, "beam_snapshots_primed", False):
            emu.memory.prime_beam_snapshots_from_current_vic()
        ui._render_frame_beam()
    else:
        ui._render_frame_latched()

    buf = bytes(ui._rgb_frame.buf)
    if _DISPLAY["show"]:
        _present_frame(pygame, display_surface, buf, native_w, native_h, int(_DISPLAY["scale"]))
    _save_rgb_png(buf, native_w, native_h, path)
    return native_w, native_h


def render_text_panel_png(emu: object, path: Path, *, scale: int = 2) -> Tuple[int, int]:
    """Render the colored C64 text screen (Textual main panel) to PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for README screenshots. "
            "Install with: pip install -r requirements-docs.txt"
        ) from exc

    palette = c64_palette_rgb()
    cell_w, cell_h = 8 * scale, 8 * scale
    full_cols = SCREEN_COLS + BORDER_WIDTH * 2
    full_rows = SCREEN_ROWS + BORDER_HEIGHT * 2
    width = full_cols * cell_w
    height = full_rows * cell_h
    img = Image.new("P", (width, height), 0)
    flat_palette = []
    for r, g, b in palette:
        flat_palette.extend((r, g, b))
    while len(flat_palette) < 768:
        flat_palette.extend((0, 0, 0))
    img.putpalette(flat_palette)

    bg = emu.memory.peek_vic(0x21) & 0x0F
    border = emu.memory.peek_vic(0x20) & 0x0F

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size=7 * scale)
    except OSError:
        font = ImageFont.load_default()

    with emu.screen_lock:
        for row in range(full_rows):
            for col in range(full_cols):
                x0 = col * cell_w
                y0 = row * cell_h
                if (
                    row < BORDER_HEIGHT
                    or row >= BORDER_HEIGHT + SCREEN_ROWS
                    or col < BORDER_WIDTH
                    or col >= BORDER_WIDTH + SCREEN_COLS
                ):
                    fill = border
                else:
                    sr = row - BORDER_HEIGHT
                    sc = col - BORDER_WIDTH
                    ch = str(emu.text_screen[sr][sc])
                    fg = int(emu.text_colors[sr][sc] & 0x0F)
                    rev = bool(emu.text_reversed[sr][sc])
                    if rev:
                        cell_bg, cell_fg = fg, bg
                    else:
                        cell_bg, cell_fg = bg, fg
                    draw.rectangle(
                        (x0, y0, x0 + cell_w - 1, y0 + cell_h - 1),
                        fill=int(cell_bg),
                    )
                    draw.text((x0 + scale, y0 + scale), ch, fill=int(cell_fg), font=font)
                    continue
                draw.rectangle(
                    (x0, y0, x0 + cell_w - 1, y0 + cell_h - 1),
                    fill=int(border),
                )

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return width, height


def _deep_merge(base: dict, override: Mapping[str, Any]) -> dict:
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, Mapping) and isinstance(out.get(key), Mapping):
            merged = dict(out[key])
            merged.update(val)  # type: ignore[arg-type]
            out[key] = merged
        else:
            out[key] = val
    return out


def _entry_render_png(
    emu: object,
    path: Path,
    render: Mapping[str, Any],
    *,
    extra_cycles: int = 0,
) -> Tuple[int, int]:
    mode = _normalize_render_mode(render.get("mode", "latched"))
    border = int(render.get("border_size", 32))
    iface = str(render.get("interface", "graphics"))
    if extra_cycles > 0:
        run_to_cycles(emu, int(emu.current_cycles) + extra_cycles)
    if iface == "textual":
        return render_textual_ui_png(emu, path)
    return render_pygame_frame(emu, path, cycles=0, mode=mode, border_size=border)


def _setup_drive_from_d64(emu: object, d64_path: Path, rom_dir: Path, device: int) -> None:
    emu.initialize_iec_bus(tcp_drives=None)
    dos_rom = rom_dir / "dos1541"
    emu._spawn_local_drive(
        str(d64_path),
        device=device,
        tier="fast",
        dos_rom_path=str(dos_rom) if dos_rom.exists() else None,
    )
    emu.kernal_load_shortcut_enabled = True
    emu._auto_spawned_drive = False


def _load_media(
    emu: object,
    media_path: Path,
    rom_dir: Path,
    entry: Mapping[str, Any],
) -> None:
    suffix = media_path.suffix.lower()
    if suffix == ".prg":
        emu.load_prg(str(media_path))
        if entry.get("auto_run", True):
            emu._inject_run_command()
        return
    if suffix == ".d64":
        device = int(entry.get("attach_drive", 8))
        _setup_drive_from_d64(emu, media_path, rom_dir, device)
        load_name = entry.get("load_prg")
        if load_name:
            # KERNAL-style load via injected LOAD (device 8 default in string).
            cmd = f'LOAD"{load_name}",{device}'.upper()
            for ch in cmd:
                emu.send_petscii(ord(ch))
            emu.send_petscii(13)
            if entry.get("auto_run", True):
                emu._inject_run_command()
        return
    raise ValueError(f"unsupported media type: {media_path}")


def _normalize_key_specs(raw: Any) -> List[Tuple[int, str]]:
    """Normalize the manifest ``keys`` field into sorted ``(cycle_offset, payload)``.

    Accepts a list of ``{"cycles": int, "press": str}`` specs, or a bare string
    (fired at offset 0). Offsets are relative to media-load completion (start of
    the ``after_run_cycles`` window).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [(0, raw)]
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("'keys' must be a string or a list of {cycles, press} specs")

    specs: List[Tuple[int, str]] = []
    for item in raw:
        if isinstance(item, str):
            specs.append((0, item))
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"invalid keys spec: {item!r}")
        payload = item.get("press", item.get("keys", ""))
        specs.append((int(item.get("cycles", 0)), str(payload)))
    specs.sort(key=lambda s: s[0])
    return specs


def _inject_key_payload(emu: object, payload: str) -> None:
    """Enqueue a ``--inject-keys`` style payload into the KERNAL keyboard queue."""
    kb, joy1, joy2, _hold = _expand_inject_payload(payload)
    if joy1 or joy2:
        print(
            "WARN: joystick tokens in 'keys' are ignored during capture "
            "(keyboard only)",
            file=sys.stderr,
        )
    dropped = 0
    for byte in kb:
        if not emu.send_petscii(int(byte)):
            dropped += 1
    if dropped:
        print(f"WARN: {dropped} key byte(s) dropped (keyboard buffer full)", file=sys.stderr)


def _run_after_load_with_keys(
    emu: object,
    *,
    after_run: int,
    key_specs: Sequence[Tuple[int, str]],
) -> None:
    """Run the after-load window, injecting keypresses at relative cycle offsets."""
    load_done = int(emu.current_cycles)
    end_target = load_done + after_run
    for offset, payload in key_specs:
        run_to_cycles(emu, load_done + max(offset, 0))
        _inject_key_payload(emu, payload)
    run_to_cycles(emu, max(end_target, int(emu.current_cycles)))


def _emu_clock_hz(emu: object) -> int:
    """PAL/NTSC master-clock frequency for the emulator's video standard."""
    standard = str(getattr(getattr(emu, "memory", None), "video_standard", "pal"))
    return NTSC_CLOCK_HZ if standard.lower() == "ntsc" else PAL_CLOCK_HZ


def _fmt_t(cycle_offset: int, clock_hz: int) -> str:
    """Format a cycle offset as a human-friendly ``+1.50s (1234567c)`` string."""
    secs = cycle_offset / float(clock_hz)
    return f"+{secs:.2f}s ({cycle_offset:,}c)"


def _attach_resid(emu: object, *, sample_rate: int = 44_100) -> Optional[object]:
    """Attach an offline (no pygame mixer) reSID so SID writes are captured.

    Returns the SID instance, or ``None`` if the reSID library is unavailable.
    """
    try:
        resid_mod = import_module(f"{_PKG}.resid")
    except ImportError as exc:
        print(f"WARN: reSID unavailable, audio disabled: {exc}", file=sys.stderr)
        return None
    standard = str(getattr(emu.memory, "video_standard", "pal"))
    try:
        sid = resid_mod.ReSIDEmulator(
            video_standard=standard,
            sample_rate=sample_rate,
            cpu_lockstep=True,
            start_audio=False,
        )
    except Exception as exc:  # noqa: BLE001 - library/build issues
        print(f"WARN: could not init reSID, audio disabled: {exc}", file=sys.stderr)
        return None
    emu.sid = sid
    emu.memory.sid = sid
    return sid


class FrameRenderer:
    """Persistent pygame interface for fast repeated RGB grabs of the C64 frame."""

    def __init__(self, emu: object, *, mode: str = "latched", border_size: int = 32) -> None:
        self.show = _DISPLAY["show"]
        self.scale = int(_DISPLAY["scale"])
        if not self.show:
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        pygame = __import__("pygame")
        if not pygame.get_init():
            pygame.init()
        graphics_mod = import_module(f"{_PKG}.graphics")
        ui = graphics_mod.PygameInterface(emu, scale=1, fps=30, border_size=border_size)
        ui._pygame = pygame
        self.pygame = pygame
        ui._display_surface = pygame.display.set_mode((1, 1))
        ui._setup_surfaces()
        self.width, self.height = ui._native_size
        if self.show:
            self.display = pygame.display.set_mode(
                (self.width * self.scale, self.height * self.scale)
            )
            ui._display_surface = self.display
            pygame.display.set_caption("c64py capture")
        else:
            self.display = ui._display_surface
        self.emu = emu
        self.ui = ui
        self.mode = _normalize_render_mode(mode)
        self.overlay_lines: List[str] = []  # live --show HUD (input indicators)
        self._font = None
        if self.mode == "per-cycle":
            emu.memory.per_cycle_render_enabled = True
            emu.memory.ensure_per_cycle_buffers()
        elif self.mode == "beam":
            emu.memory.beam_render_enabled = True
            emu.memory.ensure_beam_buffers()

    def grab(self) -> bytes:
        if self.mode == "per-cycle":
            # Per-cycle VIC samples are captured live during the accurate VIC
            # tick (cpu per_cycle_capture_vic_sample / Rust batch). Only fall
            # back to priming from one current-VIC sample when no run history
            # exists yet; priming otherwise overwrites the real per-column data.
            if not getattr(self.emu.memory, "per_cycle_snapshots_primed", False):
                self.emu.memory.prime_per_cycle_snapshots_from_current_vic()
            self.ui._render_frame_per_cycle()
        elif self.mode == "beam":
            # Per-line VIC snapshots are captured live during emulation
            # (cpu.beam_capture_raster_line). Only fall back to priming from a
            # single current-VIC sample when no run history exists yet —
            # priming otherwise overwrites real per-line data with one sample
            # taken wherever we stopped, which corrupts raster-split frames.
            if not getattr(self.emu.memory, "beam_snapshots_primed", False):
                self.emu.memory.prime_beam_snapshots_from_current_vic()
            self.ui._render_frame_beam()
        else:
            self.ui._render_frame_latched()
        rgb = bytes(self.ui._rgb_frame.buf)
        if self.show:
            self._present(rgb)
        return rgb

    def _present(self, rgb: bytes) -> None:
        pg = self.pygame
        surf = pg.image.frombuffer(rgb, (self.width, self.height), "RGB")
        if self.scale != 1:
            surf = pg.transform.scale(
                surf, (self.width * self.scale, self.height * self.scale)
            )
        self.display.blit(surf, (0, 0))
        if self.overlay_lines:
            self._draw_overlay(self.overlay_lines)
        pg.display.flip()
        for _ev in pg.event.get():
            pass

    def _draw_overlay(self, lines: Sequence[str]) -> None:
        pg = self.pygame
        if self._font is None:
            if not pg.font.get_init():
                pg.font.init()
            self._font = pg.font.Font(None, max(16, 8 * self.scale))
        pad = 4
        y = self.display.get_height() - pad
        for text in reversed(list(lines)):
            txt = self._font.render(text, True, (255, 255, 0))
            box_h = txt.get_height() + pad
            bg = pg.Surface((txt.get_width() + 2 * pad, box_h))
            bg.set_alpha(170)
            bg.fill((0, 0, 0))
            self.display.blit(bg, (pad, y - box_h))
            self.display.blit(txt, (2 * pad, y - box_h + pad // 2))
            y -= box_h + 2

    def save_png(self, path: Path) -> None:
        _save_rgb_png(self.grab(), self.width, self.height, path)


def _script_needs_audio(events: Sequence[Any], merged: Mapping[str, Any]) -> bool:
    if bool(merged.get("audio", False)):
        return True
    return any(
        e.kind == "video" and e.video is not None and e.video.audio for e in events
    )


def _run_script_timeline(
    emu: object,
    events: Sequence[Any],
    *,
    local_dir: Path,
    render: Mapping[str, Any],
    sid: Optional[object],
) -> List[Path]:
    """Execute a parsed timeline: inject keys, take screenshots, record videos.

    Timestamps are offsets from media-load completion (current cycle at entry).
    """
    interface = str(render.get("interface", "graphics"))
    mode = _normalize_render_mode(render.get("mode", "latched"))
    border = int(render.get("border_size", 32))
    sample_rate = int(getattr(sid, "_sample_rate", 44_100)) if sid is not None else 44_100
    clock_hz = _emu_clock_hz(emu)
    load_done = int(emu.current_cycles)

    renderer: Optional[FrameRenderer] = None

    def get_renderer() -> FrameRenderer:
        nonlocal renderer
        if renderer is None:
            renderer = FrameRenderer(emu, mode=mode, border_size=border)
        return renderer

    # Expand events into ordered atomic ops. Press ops fire before the buffer
    # key / screenshot at the same instant; releases fire after; video stop last.
    PRIO = {
        "vstart": 0, "press": 1, "key": 2, "screenshot": 3, "release": 4, "vstop": 5,
    }
    actions: List[Tuple[int, int, str, Any]] = []
    for ev in events:
        base = load_done + ev.at_cycle
        if ev.kind == "video":
            actions.append((load_done + ev.video.start_cycle, PRIO["vstart"], "vstart", ev))
            actions.append((load_done + ev.video.end_cycle, PRIO["vstop"], "vstop", ev))
        elif ev.kind == "screenshot":
            actions.append((base, PRIO["screenshot"], "screenshot", ev))
        elif ev.kind == "joystick":
            actions.append((base, PRIO["press"], "jpress", ev.joy))
            actions.append((base + ev.hold_cycles, PRIO["release"], "jrelease", ev.joy))
        elif ev.kind == "key":
            if ev.method == "matrix":
                step = ev.hold_cycles + ev.gap_cycles
                for idx, stroke in enumerate(parse_matrix_keys(ev.key)):
                    t0 = base + idx * step
                    actions.append((t0, PRIO["press"], "mpress", stroke))
                    actions.append((t0 + ev.hold_cycles, PRIO["release"], "mrelease", stroke))
            else:
                actions.append((base, PRIO["key"], "key", ev.key))
    actions.sort(key=lambda a: (a[0], a[1]))

    n_keys = sum(1 for e in events if e.kind == "key")
    n_shots = sum(1 for e in events if e.kind == "screenshot")
    n_vids = sum(1 for e in events if e.kind == "video")
    n_joy = sum(1 for e in events if e.kind == "joystick")
    audio_state = "on" if sid is not None else "off"
    print(
        f"  timeline: {len(events)} events "
        f"({n_keys} key, {n_joy} joystick, {n_shots} screenshot, {n_vids} video) "
        f"| audio {audio_state}"
    )

    outputs: List[Path] = []
    active: Optional[VideoRecorder] = None
    active_ev: Optional[Any] = None
    next_frame: Optional[int] = None
    interval: Optional[int] = None
    active_total: int = 0
    active_fps: int = 0

    # Live input HUD state: label -> expiry cycle (None = held until released).
    overlay: dict = {}
    press_since: dict = {}  # label -> press cycle, for console hold timing

    def overlay_now(cyc: int) -> List[str]:
        for lbl in [l for l, exp in overlay.items() if exp is not None and cyc >= exp]:
            overlay.pop(lbl, None)
        return list(overlay.keys())

    def emit_frame() -> None:
        rend = get_renderer()
        rend.overlay_lines = overlay_now(int(emu.current_cycles))
        rgb = rend.grab()
        if active is not None:
            if sid is not None:
                active.write_audio(sid.drain_pcm())
            active.write_frame(rgb)
            # Progress roughly once per recorded second.
            if active_fps and active._frames % active_fps == 0:
                secs = active._frames / float(active_fps)
                print(f"    ... recording {secs:.0f}s ({active._frames}/{active_total} frames)")

    def advance_to(target: int) -> None:
        nonlocal next_frame
        if active is None or interval is None or next_frame is None:
            run_to_cycles(emu, target)
            return
        while next_frame <= target:
            run_to_cycles(emu, next_frame)
            emit_frame()
            next_frame += interval
        run_to_cycles(emu, target)

    for cyc, prio, kind, data in actions:
        advance_to(cyc)
        when = _fmt_t(cyc - load_done, clock_hz)
        if kind == "key":
            print(f"  [{when}] key (buffer): {data!r}")
            _inject_key_payload(emu, data)
            overlay[f"TYPE {data}"] = int(cyc + 0.4 * clock_hz)
        elif kind == "mpress":
            lbl = f"KEY {data.label}"
            print(f"  [{when}] key down (matrix): {data.label}")
            for row, col in data.cells:
                emu.memory.press_matrix_key(row, col)
            overlay[lbl] = None
            press_since[lbl] = cyc
        elif kind == "mrelease":
            lbl = f"KEY {data.label}"
            for row, col in data.cells:
                emu.memory.release_matrix_key(row, col)
            overlay.pop(lbl, None)
            held = (cyc - press_since.pop(lbl, cyc)) / float(clock_hz)
            print(f"  [{when}] key up   (matrix): {data.label} (held {held:.2f}s)")
        elif kind == "jpress":
            lbl = f"JOY{data.port} {data.label}"
            print(f"  [{when}] joystick {data.port} down: {data.label}")
            emu.memory.set_joystick_dir(data.port, data.mask)
            overlay[lbl] = None
            press_since[lbl] = cyc
        elif kind == "jrelease":
            lbl = f"JOY{data.port} {data.label}"
            emu.memory.clear_joystick_dir(data.port, data.mask)
            overlay.pop(lbl, None)
            held = (cyc - press_since.pop(lbl, cyc)) / float(clock_hz)
            print(f"  [{when}] joystick {data.port} up:   {data.label} (held {held:.2f}s)")
        elif kind == "screenshot":
            ev = data
            out = local_dir / str(ev.screenshot)
            out.parent.mkdir(parents=True, exist_ok=True)
            if interface == "textual":
                render_textual_ui_png(emu, out)
            else:
                get_renderer().save_png(out)
            print(f"  [{when}] screenshot -> {out}")
            outputs.append(out)
        elif kind == "vstart":
            ev = data
            if interface == "textual":
                print(f"  [{when}] WARN: video unsupported with textual interface; skipping", file=sys.stderr)
                continue
            if not ffmpeg_available():
                print(f"  [{when}] WARN: ffmpeg not found; skipping video", file=sys.stderr)
                continue
            if active is not None:
                print(f"  [{when}] WARN: overlapping video windows; skipping new one", file=sys.stderr)
                continue
            rend = get_renderer()
            ev.video.output = str(local_dir / ev.video.output)
            active = VideoRecorder(ev.video, sample_rate=sample_rate)
            active_ev = ev
            active.start(rend.width, rend.height)
            if sid is not None:
                sid.clear_pcm()
            interval = max(1, int(round(clock_hz / float(ev.video.fps))))
            active_fps = int(ev.video.fps)
            dur_cyc = ev.video.end_cycle - ev.video.start_cycle
            active_total = max(1, int(round(dur_cyc / float(interval))))
            next_frame = cyc + interval
            dur_s = dur_cyc / float(clock_hz)
            print(
                f"  [{when}] video START -> {ev.video.output} "
                f"({rend.width}x{rend.height} @ {active_fps}fps, {dur_s:.1f}s, "
                f"audio {'on' if (sid is not None and ev.video.audio) else 'off'})"
            )
            emit_frame()  # frame at window start
        elif kind == "vstop":
            if active is None or active_ev is not data:
                continue
            frames = active._frames
            print(f"  [{when}] video STOP, encoding {frames} frames...")
            out = active.stop()
            size = out.stat().st_size if out.is_file() else 0
            print(f"  [{when}] video DONE -> {out} ({size:,} bytes)")
            outputs.append(out)
            active = None
            active_ev = None
            next_frame = None
            interval = None
            active_total = 0
            active_fps = 0

    if active is not None:  # safety: unterminated window
        print("  WARN: timeline ended with video still recording; finalizing", file=sys.stderr)
        outputs.append(active.stop())
    # Drop any keys/joystick still held (defensive; releases are normally scheduled).
    if hasattr(emu.memory, "release_all_keys"):
        emu.memory.release_all_keys()
    if hasattr(emu.memory, "release_all_joystick"):
        emu.memory.release_all_joystick()
    return outputs


def capture_entry(
    entry: Mapping[str, Any],
    *,
    local_dir: Path,
    rom_dir: Path,
    defaults: Mapping[str, Any],
) -> List[Path]:
    """Run one manifest entry; return list of PNG paths written."""
    merged = _deep_merge(dict(defaults), entry)
    entry_id = str(merged.get("id", merged.get("media", "entry")))
    media_name = merged.get("media")
    if not media_name:
        raise ValueError(f"entry {entry_id!r} missing 'media'")
    media_path = (local_dir / str(media_name)).resolve()
    if not media_path.is_file():
        if merged.get("skip_if_missing", True):
            print(f"SKIP: {entry_id} ({media_name} not found)")
            return []
        raise FileNotFoundError(media_path)

    boot_cycles = int(merged.get("boot_cycles", 5_000_000))
    after_run = int(merged.get("after_run_cycles", 10_000_000))
    vic, vic_note = _normalize_vic(merged.get("vic_emulation", "fast"))
    if vic_note:
        print(f"[{entry_id}] NOTE: {vic_note}")
    turbo = bool(merged.get("turbo", True))
    render = merged.get("render") or {}
    if not isinstance(render, Mapping):
        render = {}
    r_interface = str(render.get("interface", "graphics"))
    r_mode = _normalize_render_mode(render.get("mode", "latched"))
    r_border = int(render.get("border_size", 32))

    script_raw = merged.get("script")
    key_specs = _normalize_key_specs(merged.get("keys"))

    emu = make_emu(vic_emulation=vic, turbo=turbo)
    clock_hz = _emu_clock_hz(emu)
    std = str(getattr(emu.memory, "video_standard", "pal")).upper()

    # Capture per-line / per-cycle VIC snapshots for the whole run so accurate
    # compositing reflects raster splits (status bars, color bars) and mid-line
    # register changes on every grabbed frame.
    if r_interface != "textual":
        if r_mode == "per-cycle":
            emu.memory.per_cycle_render_enabled = True
            emu.memory.ensure_per_cycle_buffers()
        elif r_mode == "beam":
            emu.memory.beam_render_enabled = True
            emu.memory.ensure_beam_buffers()

    sid = None
    events: List[Any] = []
    if script_raw:
        events = parse_script(
            script_raw, clock_hz=clock_hz, cycles_per_frame=PAL_CYCLES_PER_FRAME
        )
        if _script_needs_audio(events, merged):
            sid = _attach_resid(emu)

    print(
        f"[{entry_id}] config: vic_emulation={emu.vic_emulation} video={std} "
        f"clock={clock_hz}Hz turbo={'on' if turbo else 'off'} | "
        f"render interface={r_interface} mode={r_mode} border={r_border} | "
        f"audio={'on' if sid is not None else 'off'} "
        f"display={'on' if show_enabled() else 'off'}"
    )
    if r_interface != "textual":
        if r_mode == "latched" and emu.vic_emulation != "fast":
            print(
                f"[{entry_id}] HINT: vic_emulation is accurate but render mode is 'latched' "
                f"(per-frame). For demos with raster splits/mid-frame VIC changes, set "
                f'render.mode to "per-cycle" (matches the live per-cycle path) or "beam".'
            )
        elif r_mode == "per-cycle" and emu.vic_emulation == "fast":
            print(
                f"[{entry_id}] HINT: render mode is 'per-cycle' but vic_emulation is 'fast', "
                f"which does not capture per-cycle VIC samples; frames will fall back to "
                f'latched. Set vic_emulation to "accurate-rust" (or "accurate-python") to '
                f"get true per-cycle compositing."
            )

    load_roms(emu, rom_dir)
    emu.running = True
    run_to_cycles(emu, boot_cycles)
    _load_media(emu, media_path, rom_dir, merged)

    # Timeline mode supersedes the legacy after_run/keys/frames flow.
    if events:
        print(f"[{entry_id}] media loaded at {int(emu.current_cycles):,} cycles; starting timeline")
        try:
            outs = _run_script_timeline(
                emu, events, local_dir=local_dir, render=render, sid=sid
            )
        finally:
            if sid is not None:
                sid.close()
        if not outs:
            print(f"NOTE: {entry_id} script produced no outputs")
        return outs

    _run_after_load_with_keys(emu, after_run=after_run, key_specs=key_specs)

    outputs: List[Path] = []
    main_out = local_dir / str(merged.get("output", f"output/{entry_id}.png"))
    main_out.parent.mkdir(parents=True, exist_ok=True)
    _entry_render_png(emu, main_out, render)
    print(f"wrote {main_out} ({entry_id})")
    outputs.append(main_out)

    frames = merged.get("frames")
    if isinstance(frames, list) and frames:
        with tempfile.NamedTemporaryFile(suffix=".snap", delete=False) as tmp:
            snap_path = Path(tmp.name)
        try:
            save_snap(emu, snap_path, note=f"local {entry_id}")
            base_cycles = int(emu.current_cycles)
            for spec in frames:
                if not isinstance(spec, Mapping):
                    continue
                rel = int(spec.get("cycles", 0))
                frame_out = local_dir / str(
                    spec.get("output", f"output/{entry_id}_c{rel}.png")
                )
                frame_out.parent.mkdir(parents=True, exist_ok=True)
                frame_emu = make_emu(vic_emulation=vic, turbo=turbo)
                load_roms(frame_emu, rom_dir)
                load_snap(frame_emu, snap_path)
                frame_emu.running = True
                target = base_cycles + rel
                if target > int(frame_emu.current_cycles):
                    run_to_cycles(frame_emu, target)
                _entry_render_png(frame_emu, frame_out, render)
                print(f"wrote {frame_out} ({entry_id} frame +{rel} cycles)")
                outputs.append(frame_out)
        finally:
            snap_path.unlink(missing_ok=True)

    return outputs


def run_manifest_entries(
    manifest: Mapping[str, Any],
    *,
    local_dir: Path,
    rom_dir: Path,
    dry_run: bool = False,
    only_ids: Optional[Sequence[str]] = None,
) -> List[Path]:
    defaults = manifest.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        defaults = {}
    entries = manifest.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError("manifest 'entries' must be a list")

    wanted = set(only_ids) if only_ids else None
    matched: set = set()

    outputs: List[Path] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        entry_id = str(raw.get("id", raw.get("media", "?")))
        if wanted is not None and entry_id not in wanted:
            continue
        matched.add(entry_id)
        media_name = raw.get("media")
        media_path = local_dir / str(media_name) if media_name else None
        if dry_run:
            status = "missing" if media_path and not media_path.is_file() else "ready"
            script = raw.get("script")
            if isinstance(script, list):
                n_vid = sum(1 for s in script if isinstance(s, Mapping)
                            and ("video" in s or "take_video" in s))
                target = f"script: {len(script)} events ({n_vid} video)"
            else:
                target = str(raw.get("output"))
            print(f"PLAN: {entry_id} -> {target} [{status}]")
            continue
        try:
            outputs.extend(
                capture_entry(raw, local_dir=local_dir, rom_dir=rom_dir, defaults=defaults)
            )
        except Exception as exc:
            print(f"WARN: {entry_id} failed: {exc}")

    if wanted is not None:
        missing = wanted - matched
        if missing:
            print(f"WARN: no manifest entries matched id(s): {', '.join(sorted(missing))}")
    return outputs


def render_textual_ui_png(emu: object, path: Path) -> Tuple[int, int]:
    """Compose a Textual-style chrome frame around the C64 text panel."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for README screenshots. "
            "Install with: pip install -r requirements-docs.txt"
        ) from exc

    panel_path = path.with_suffix(".panel.tmp.png")
    pw, ph = render_text_panel_png(emu, panel_path, scale=1)
    panel = Image.open(panel_path).convert("P")
    panel_path.unlink(missing_ok=True)

    header_h = 18
    status_h = 16
    margin = 4
    width = pw + margin * 2
    height = header_h + ph + status_h + margin * 3
    canvas = Image.new("P", (width, height), 0)
    flat_palette = []
    for r, g, b in c64_palette_rgb():
        flat_palette.extend((r, g, b))
    flat_palette.extend((30, 30, 30, 45, 45, 45, 20, 20, 20, 180, 220, 180, 160, 160, 160, 220, 220, 220))
    while len(flat_palette) < 768:
        flat_palette.extend((0, 0, 0))
    canvas.putpalette(flat_palette)

    chrome_bg = 16
    chrome_bar = 17
    chrome_debug = 18
    chrome_text = 19
    chrome_status = 20

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size=11)
        small = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size=9)
    except OSError:
        font = ImageFont.load_default()
        small = font

    draw.rectangle((0, 0, width, height), fill=chrome_bg)
    draw.text((margin, 2), "c64py — Textual interface", fill=chrome_text, font=font)
    canvas.paste(panel, (margin, header_h))
    y_status = header_h + ph + margin
    draw.rectangle((margin, y_status, width - margin, y_status + status_h), fill=chrome_bar)
    cycles = int(getattr(emu, "current_cycles", 0))
    draw.text(
        (margin + 4, y_status + 2),
        f"running | cycles {cycles:,} | Ctrl+X quit | F12 snapshot",
        fill=chrome_status,
        font=small,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="PNG", optimize=True)
    return width, height
