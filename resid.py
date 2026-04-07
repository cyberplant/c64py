"""
ReSID audio emulation using the VICE-Team reSID C++ library via ctypes.

reSID is a cycle-accurate reverse-engineered emulation of the MOS 6581/8580
SID chip.  This module loads a thin C wrapper shared library (resid_c.so /
resid_c.dylib) built from ``src/resid_wrapper/`` and exposes a
``ReSIDEmulator`` class with the same interface as ``SidEmulator`` in
``sid.py``.

Building the shared library
---------------------------
See ``src/resid_wrapper/README.md`` for full instructions.  Quick start::

    cd src/resid_wrapper
    make RESID_SYSTEM=1   # if libresid-builder-dev is installed
    make install          # copies resid_c.so next to this file

Runtime library search order
-----------------------------
1. The ``c64py`` package directory (same directory as this file).
2. Paths listed in the ``RESID_LIB_PATH`` environment variable
   (colon-separated on POSIX, semicolon on Windows).
3. Standard OS library search paths (``LD_LIBRARY_PATH``, etc.).

Audio queue diagnostics (lockstep / Rust PCM path)
----------------------------------------------------
Set ``C64PY_RESID_TRACE=1`` to emit rate-limited ``C64PY_RESID_AUDIO`` JSON lines to stderr
when the pending PCM queue **underruns** (mixer asked for a full buffer but less PCM was
ready, so silence was padded) or **overruns** (pending bytes exceeded the cap and oldest
samples were dropped). Optional ``C64PY_RESID_TRACE_INTERVAL`` (seconds, default ``1``)
controls the minimum gap between lines. A final summary line is printed on ``close()``
when tracing is enabled. Use ``ReSIDEmulator.get_resid_audio_stats()`` for live totals
without stderr noise.

WAV capture (same PCM as pygame)
----------------------------------
Set ``C64PY_RESID_WAV`` to a filesystem path (e.g. ``/tmp/c64_resid.wav``). Every mixer
buffer produced by ``_render_buffer`` — the **same** int16 mono PCM sent to pygame,
including underrun silence — is appended. On ``close()``, a WAV file is written (sample
rate matches the emulator, default 44100 Hz). Optional ``C64PY_RESID_WAV_MAX_SEC`` caps
how much audio is kept (float seconds).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import signal
import sys
import threading
import time
import warnings
import wave
from typing import Any, Dict, Optional


def _env_resid_trace() -> bool:
    v = os.environ.get("C64PY_RESID_TRACE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _resid_trace_interval_sec() -> float:
    try:
        x = float(os.environ.get("C64PY_RESID_TRACE_INTERVAL", "1.0").strip())
    except ValueError:
        x = 1.0
    return max(0.05, x)


def _env_resid_wav_path() -> Optional[str]:
    p = os.environ.get("C64PY_RESID_WAV", "").strip()
    return p if p else None


# ---------------------------------------------------------------------------
# Library loading helpers
# ---------------------------------------------------------------------------

def _find_resid_lib() -> Optional[str]:
    """Search for the resid_c shared library and return its path, or None."""
    lib_names = ["resid_c"]
    suffixes = [".so", ".dylib", ".dll"]

    # 1. Package directory (same dir as this file)
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    for name in lib_names:
        for suffix in suffixes:
            candidate = os.path.join(pkg_dir, name + suffix)
            if os.path.isfile(candidate):
                return candidate

    # 2. Paths from environment variable
    env_paths = os.environ.get("RESID_LIB_PATH", "")
    sep = ";" if sys.platform == "win32" else ":"
    for directory in env_paths.split(sep):
        directory = directory.strip()
        if not directory:
            continue
        for name in lib_names:
            for suffix in suffixes:
                candidate = os.path.join(directory, name + suffix)
                if os.path.isfile(candidate):
                    return candidate

    # 3. Standard OS search (ctypes.util.find_library)
    path = ctypes.util.find_library("resid_c")
    if path:
        return path

    return None


def find_resid_lib() -> Optional[str]:
    """Public helper for Rust batch path."""
    return _find_resid_lib()


def _load_resid_lib() -> ctypes.CDLL:
    """Load the resid_c shared library and set up ctypes signatures.

    Raises:
        ImportError: if the library cannot be found or loaded.
    """
    path = _find_resid_lib()
    if path is None:
        raise ImportError(
            "Could not find the resid_c shared library (resid_c.so / "
            "resid_c.dylib).  Build it from src/resid_wrapper/ and copy it "
            "to the c64py package directory, or set the RESID_LIB_PATH "
            "environment variable.  See src/resid_wrapper/README.md for "
            "details."
        )

    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise ImportError(f"Failed to load reSID wrapper library '{path}': {exc}") from exc

    # resid_sid_t* resid_create(void)
    lib.resid_create.restype = ctypes.c_void_p
    lib.resid_create.argtypes = []

    # void resid_destroy(resid_sid_t*)
    lib.resid_destroy.restype = None
    lib.resid_destroy.argtypes = [ctypes.c_void_p]

    # void resid_set_chip_model(resid_sid_t*, int)
    lib.resid_set_chip_model.restype = None
    lib.resid_set_chip_model.argtypes = [ctypes.c_void_p, ctypes.c_int]

    # void resid_reset(resid_sid_t*)
    lib.resid_reset.restype = None
    lib.resid_reset.argtypes = [ctypes.c_void_p]

    # uint8_t resid_read(resid_sid_t*, uint8_t)
    lib.resid_read.restype = ctypes.c_uint8
    lib.resid_read.argtypes = [ctypes.c_void_p, ctypes.c_uint8]

    # void resid_write(resid_sid_t*, uint8_t, uint8_t)
    lib.resid_write.restype = None
    lib.resid_write.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8]

    # int resid_set_sampling_parameters(resid_sid_t*, double, int, double, double)
    lib.resid_set_sampling_parameters.restype = ctypes.c_int
    lib.resid_set_sampling_parameters.argtypes = [
        ctypes.c_void_p,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_double,
    ]

    # int resid_clock(resid_sid_t*, int*, int16_t*, int)
    lib.resid_clock.restype = ctypes.c_int
    lib.resid_clock.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
    ]

    return lib


# ---------------------------------------------------------------------------
# Sampling method constants (mirror resid_c.h)
# ---------------------------------------------------------------------------
SAMPLE_FAST = 0
SAMPLE_INTERPOLATE = 1
SAMPLE_RESAMPLE = 2
SAMPLE_RESAMPLE_FASTMEM = 3

# Chip model constants
MOS6581 = 0
MOS8580 = 1


# ---------------------------------------------------------------------------
# ReSIDEmulator
# ---------------------------------------------------------------------------

class ReSIDEmulator:
    """SID emulator backed by the VICE-Team reSID C++ library.

    Provides the same interface as :class:`sid.SidEmulator` so it can be
    used as a drop-in replacement.  Audio output is streamed via
    ``pygame.mixer``.

    The reSID shared library (``resid_c.so``) must be built and installed
    before this class can be instantiated.  See ``src/resid_wrapper/README.md``
    for build instructions.

    Args:
        video_standard: ``"pal"`` (default) or ``"ntsc"``.
        sample_rate:    Audio output sample rate in Hz (default 44100).
        buffer_ms:      Audio buffer length in milliseconds (default 50).
        mixer_buffer:   pygame mixer buffer size in samples (default 512).
        chip_model:     SID chip model: ``"6581"`` (default) or ``"8580"``.
                        Can also be set via the ``RESID_CHIP_MODEL``
                        environment variable.
        sampling_method: reSID sampling method (default
                        :data:`SAMPLE_INTERPOLATE`).
        cpu_lockstep:   If ``True`` (default), advance the SID from the CPU thread via
                        :meth:`tick_cpu_cycles` so reads like ``$D41B`` match cycle-accurate
                        hosts. If ``False`` (used with **fast** coarse VIC), the audio
                        thread advances reSID in large ``resid_clock`` steps only—much
                        faster, but SID phase vs CPU is approximate.
    """

    REG_COUNT = 0x20

    def __init__(
        self,
        *,
        video_standard: str = "pal",
        sample_rate: int = 44100,
        buffer_ms: int = 50,
        mixer_buffer: int = 512,
        chip_model: Optional[str] = None,
        sampling_method: int = SAMPLE_INTERPOLATE,
        cpu_lockstep: bool = True,
    ) -> None:
        self._lib = _load_resid_lib()
        self._sid_ptr = self._lib.resid_create()
        if not self._sid_ptr:
            raise RuntimeError("resid_create() returned NULL – out of memory?")

        self._lock = threading.Lock()
        self._sample_rate = int(sample_rate)
        self._buffer_samples = max(64, int(self._sample_rate * buffer_ms / 1000))
        self._buffer_seconds = self._buffer_samples / self._sample_rate
        self._sampling_method = sampling_method

        # Clock frequency
        self._clock_hz = self._clock_for_standard(video_standard)

        # Chip model (env var overrides argument)
        env_model = os.environ.get("RESID_CHIP_MODEL", "")
        if env_model.strip() in ("8580", "MOS8580"):
            model_const = MOS8580
        elif chip_model == "8580":
            model_const = MOS8580
        else:
            model_const = MOS6581

        self._lib.resid_set_chip_model(self._sid_ptr, model_const)

        self._cpu_lockstep = bool(cpu_lockstep)

        # Apply sampling parameters
        ok = self._lib.resid_set_sampling_parameters(
            self._sid_ptr,
            float(self._clock_hz),
            self._sampling_method,
            float(self._sample_rate),
            -1.0,
        )
        if not ok:
            warnings.warn(
                "reSID: set_sampling_parameters() failed – "
                "audio may be degraded.",
                RuntimeWarning,
            )

        # Target C64 clocks per mixer buffer (audio drains PCM produced by CPU-driven clock).
        self._cycles_per_buffer = self._clock_hz * self._buffer_seconds

        # Scratch buffer for resid_clock(); must stay non-empty capacity (wrapper rejects 0).
        self._clock_scratch_samples = 4096
        self._clock_scratch = (ctypes.c_int16 * self._clock_scratch_samples)()

        # PCM queued from tick_cpu_cycles() when cpu_lockstep (accurate path).
        self._pcm_pending = bytearray()
        self._pcm_pending_max = self._sample_rate * 2 * 8  # cap ~8 s mono int16 bytes
        self._pcm_pending_peak = 0  # max len(_pcm_pending) seen (producer-side), under _lock
        # True when Rust batch is feeding PCM (drain queue even if cpu_lockstep is False).
        self._rust_pcm_mode = False

        # Lockstep queue diagnostics (underrun = padded silence; overrun = dropped front samples).
        self._stats_lock = threading.Lock()
        self._resid_underrun_buffers = 0
        self._resid_underrun_pad_samples = 0
        self._resid_overrun_events = 0
        self._resid_overrun_drop_bytes = 0
        self._trace_last_emit = 0.0
        self._trace_window_underrun_buffers = 0
        self._trace_window_overrun_events = 0
        self._trace_window_drop_bytes = 0

        # Legacy field (audio thread no longer clocks SID directly).
        self._cycle_remainder = 0

        # PCM output buffer (audio thread: lockstep drain or resid_clock in decoupled mode)
        self._pcm_buf = (ctypes.c_int16 * self._buffer_samples)()
        self._current_sound = None  # keep Sound alive while playing
        self._queued_sound = None   # keep queued Sound alive

        # pygame mixer
        self._pygame = None
        self._channel = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Optional WAV capture: same buffers as pygame (see module docstring).
        self._wav_path: Optional[str] = _env_resid_wav_path()
        self._wav_buf: Optional[bytearray] = (
            bytearray() if self._wav_path else None
        )
        self._wav_max_bytes: Optional[int] = None
        if self._wav_buf is not None:
            raw_max = os.environ.get("C64PY_RESID_WAV_MAX_SEC", "").strip()
            if raw_max:
                try:
                    sec = float(raw_max)
                    self._wav_max_bytes = max(0, int(sec * self._sample_rate * 2))
                except ValueError:
                    self._wav_max_bytes = None

        self._init_audio(mixer_buffer)

    # ------------------------------------------------------------------
    # Lockstep queue stats / trace (underrun & overrun)
    # ------------------------------------------------------------------

    def get_resid_audio_stats(self) -> Dict[str, Any]:
        """Snapshot of PCM queue diagnostics (lockstep or ``extend_pcm_from_rust`` path).

        * **underrun_buffers** — ``_render_buffer`` calls that padded with silence.
        * **underrun_pad_samples** — total mono samples replaced by padding.
        * **overrun_events** — times the pending queue was trimmed after exceeding the cap.
        * **overrun_drop_bytes** — total bytes removed from the front (int16 mono PCM).
        """
        # Always take _lock before _stats_lock (same order as trace helpers) to avoid deadlock.
        with self._lock:
            pending_now = len(self._pcm_pending) if self._sid_ptr else 0
            peak = self._pcm_pending_peak
        with self._stats_lock:
            return {
                "underrun_buffers": self._resid_underrun_buffers,
                "underrun_pad_samples": self._resid_underrun_pad_samples,
                "overrun_events": self._resid_overrun_events,
                "overrun_drop_bytes": self._resid_overrun_drop_bytes,
                "pending_bytes_now": pending_now,
                "pending_peak_bytes": peak,
                "pending_cap_bytes": self._pcm_pending_max,
                "buffer_samples": self._buffer_samples,
                "wav_path": self._wav_path,
                "wav_recorded_bytes": (
                    len(self._wav_buf) if self._wav_buf is not None else None
                ),
            }

    def _note_lockstep_underrun(self, pad_samples: int) -> None:
        if pad_samples <= 0:
            return
        with self._stats_lock:
            self._resid_underrun_buffers += 1
            self._resid_underrun_pad_samples += pad_samples
            self._trace_window_underrun_buffers += 1
        self._maybe_emit_resid_trace()

    def _note_overrun(self, drop_bytes: int, events: int = 1) -> None:
        if drop_bytes <= 0 or events <= 0:
            return
        with self._stats_lock:
            self._resid_overrun_events += events
            self._resid_overrun_drop_bytes += drop_bytes
            self._trace_window_overrun_events += events
            self._trace_window_drop_bytes += drop_bytes
        self._maybe_emit_resid_trace()

    def _maybe_emit_resid_trace(self) -> None:
        if not _env_resid_trace():
            return
        interval = _resid_trace_interval_sec()
        now = time.monotonic()
        with self._lock:
            pending_now = len(self._pcm_pending) if self._sid_ptr else 0
            peak = self._pcm_pending_peak
        with self._stats_lock:
            if now - self._trace_last_emit < interval:
                return
            if (
                self._trace_window_underrun_buffers == 0
                and self._trace_window_overrun_events == 0
            ):
                return
            wu = self._trace_window_underrun_buffers
            wo = self._trace_window_overrun_events
            wd = self._trace_window_drop_bytes
            self._trace_window_underrun_buffers = 0
            self._trace_window_overrun_events = 0
            self._trace_window_drop_bytes = 0
            self._trace_last_emit = now
            tu = self._resid_underrun_buffers
            tus = self._resid_underrun_pad_samples
            to = self._resid_overrun_events
            tob = self._resid_overrun_drop_bytes
        rec = {
            "pending_bytes_now": pending_now,
            "pending_peak_bytes": peak,
            "window_underrun_buffers": wu,
            "window_overrun_events": wo,
            "window_drop_bytes": wd,
            "total_underrun_buffers": tu,
            "total_underrun_pad_samples": tus,
            "total_overrun_events": to,
            "total_overrun_drop_bytes": tob,
        }
        print("C64PY_RESID_AUDIO " + json.dumps(rec, sort_keys=True), file=sys.stderr, flush=True)

    def _emit_resid_trace_final(self) -> None:
        if not _env_resid_trace():
            return
        with self._lock:
            pending_now = len(self._pcm_pending) if self._sid_ptr else 0
            peak = self._pcm_pending_peak
        with self._stats_lock:
            wu = self._trace_window_underrun_buffers
            wo = self._trace_window_overrun_events
            wd = self._trace_window_drop_bytes
            self._trace_window_underrun_buffers = 0
            self._trace_window_overrun_events = 0
            self._trace_window_drop_bytes = 0
            tu = self._resid_underrun_buffers
            tus = self._resid_underrun_pad_samples
            to = self._resid_overrun_events
            tob = self._resid_overrun_drop_bytes
        rec = {
            "final": True,
            "pending_bytes_now": pending_now,
            "pending_peak_bytes": peak,
            "window_underrun_buffers": wu,
            "window_overrun_events": wo,
            "window_drop_bytes": wd,
            "total_underrun_buffers": tu,
            "total_underrun_pad_samples": tus,
            "total_overrun_events": to,
            "total_overrun_drop_bytes": tob,
        }
        print("C64PY_RESID_AUDIO " + json.dumps(rec, sort_keys=True), file=sys.stderr, flush=True)

    def _wav_record_chunk(self, chunk: bytes) -> None:
        """Append PCM that was fed to pygame (audio thread only)."""
        buf = self._wav_buf
        if buf is None or not chunk:
            return
        max_b = self._wav_max_bytes
        if max_b is not None:
            room = max_b - len(buf)
            if room <= 0:
                return
            if len(chunk) > room:
                chunk = chunk[:room]
        buf.extend(chunk)

    def _flush_wav_file(self) -> None:
        """Write captured mono int16 PCM to ``C64PY_RESID_WAV`` path."""
        path = self._wav_path
        buf = self._wav_buf
        if not path or buf is None or len(buf) == 0:
            return
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self._sample_rate)
                w.writeframes(bytes(buf))
        except OSError as exc:
            warnings.warn(
                f"reSID WAV capture: could not write {path!r}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Clock / standard helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clock_for_standard(video_standard: str) -> int:
        return 985248 if video_standard == "pal" else 1022727

    @staticmethod
    def find_resid_lib() -> Optional[str]:
        """Path to resid_c shared library for Rust interop."""
        return find_resid_lib()

    def set_video_standard(self, video_standard: str) -> None:
        """Update the SID clock frequency for the given video standard."""
        self._clock_hz = self._clock_for_standard(video_standard)
        self._cycle_remainder = 0
        with self._lock:
            self._cycles_per_buffer = self._clock_hz * self._buffer_seconds
            self._lib.resid_set_sampling_parameters(
                self._sid_ptr,
                float(self._clock_hz),
                self._sampling_method,
                float(self._sample_rate),
                -1.0,
            )

    def tick_cpu_cycles(self, n: int) -> None:
        """Advance reSID by *n* C64 master-clock cycles (lockstep with CPU).

        Called from the CPU emulation thread before each bus access / time step so
        register reads (e.g. ``$D41B`` voice-3 output) see the same phase as
        cycle-accurate hosts.  PCM is appended for the audio worker to drain.

        When ``cpu_lockstep`` was set to ``False`` (fast VIC mode), this is a no-op;
        the audio thread advances the chip via :meth:`_render_buffer` instead.
        """
        if not self._cpu_lockstep or n <= 0 or not self._sid_ptr:
            return
        scratch_n = self._clock_scratch_samples
        drop_bytes = 0
        overrun_events = 0
        with self._lock:
            remaining = int(n)
            while remaining > 0:
                dt = ctypes.c_int(remaining)
                produced = self._lib.resid_clock(
                    self._sid_ptr,
                    ctypes.byref(dt),
                    self._clock_scratch,
                    scratch_n,
                )
                remaining = int(dt.value)
                if produced > 0:
                    raw = ctypes.string_at(
                        ctypes.addressof(self._clock_scratch),
                        produced * 2,
                    )
                    self._pcm_pending += raw
                    self._pcm_pending_peak = max(
                        self._pcm_pending_peak, len(self._pcm_pending)
                    )
                    while len(self._pcm_pending) > self._pcm_pending_max:
                        drop = len(self._pcm_pending) - self._pcm_pending_max
                        del self._pcm_pending[:drop]
                        drop_bytes += drop
                        overrun_events += 1
        if drop_bytes:
            self._note_overrun(drop_bytes, overrun_events)

    def rust_batch_sid_ptr(self) -> int:
        """Opaque SID pointer for the Rust fast batch."""
        return int(self._sid_ptr or 0)

    def extend_pcm_from_rust(self, pcm_bytes: bytes) -> None:
        """Append Rust-produced PCM to the lockstep queue (little-endian int16 mono)."""
        if not pcm_bytes:
            return
        drop_bytes = 0
        overrun_events = 0
        with self._lock:
            self._rust_pcm_mode = True
            self._pcm_pending += pcm_bytes
            self._pcm_pending_peak = max(
                self._pcm_pending_peak, len(self._pcm_pending)
            )
            while len(self._pcm_pending) > self._pcm_pending_max:
                drop = len(self._pcm_pending) - self._pcm_pending_max
                del self._pcm_pending[:drop]
                drop_bytes += drop
                overrun_events += 1
        if drop_bytes:
            self._note_overrun(drop_bytes, overrun_events)

    # ------------------------------------------------------------------
    # Register access
    # ------------------------------------------------------------------

    def read_register(self, offset: int) -> int:
        """Read a SID register."""
        if not 0 <= offset < self.REG_COUNT:
            return 0
        with self._lock:
            return int(self._lib.resid_read(self._sid_ptr, offset))

    def write_register(self, offset: int, value: int) -> None:
        """Write a SID register."""
        if not 0 <= offset < self.REG_COUNT:
            return
        with self._lock:
            self._lib.resid_write(self._sid_ptr, offset, value & 0xFF)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop audio playback and release reSID resources."""
        self._cycle_remainder = 0
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._emit_resid_trace_final()
        self._flush_wav_file()
        if self._channel:
            try:
                self._channel.stop()
            except Exception:
                pass
        self._current_sound = None
        self._queued_sound = None
        with self._lock:
            self._pcm_pending.clear()
        if self._sid_ptr:
            self._lib.resid_destroy(self._sid_ptr)
            self._sid_ptr = None

    # ------------------------------------------------------------------
    # Audio initialisation
    # ------------------------------------------------------------------

    def _init_audio(self, mixer_buffer: int) -> None:
        try:
            import pygame
        except ImportError as exc:
            raise RuntimeError("pygame is required for reSID audio output") from exc

        try:
            import pygame.mixer  # noqa: F401
        except Exception as exc:
            warnings.warn(
                "reSID audio disabled: 'pygame.mixer' is unavailable. "
                f"({exc})",
                RuntimeWarning,
            )
            return

        self._pygame = pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init(
                frequency=self._sample_rate,
                size=-16,
                channels=1,
                buffer=int(mixer_buffer),
            )
        self._channel = pygame.mixer.find_channel(True)
        self._running = True
        # Block pygame's C-level signal handlers BEFORE creating the thread so
        # the new thread inherits the mask with no race window.  pygame registers
        # pygame_parachute (which calls pygame.quit → SDL_DestroyWindow → Cocoa)
        # for a wide range of signals; those must only run on the main thread.
        _SIG_BLOCK = getattr(signal, 'SIG_BLOCK', None)
        _PYGAME_SIGS = {
            getattr(signal, s) for s in (
                'SIGTERM', 'SIGINT', 'SIGQUIT', 'SIGHUP',
                'SIGSEGV', 'SIGILL', 'SIGFPE', 'SIGBUS'
            ) if hasattr(signal, s)
        }
        _old_mask = None
        if _SIG_BLOCK is not None and _PYGAME_SIGS:
            try:
                _old_mask = signal.pthread_sigmask(_SIG_BLOCK, _PYGAME_SIGS)
            except (OSError, ValueError):
                pass
        self._thread = threading.Thread(target=self._audio_worker, daemon=True)
        self._thread.start()
        if _old_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, _old_mask)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Audio worker
    # ------------------------------------------------------------------

    def _audio_worker(self) -> None:
        """Background thread: render reSID output and feed pygame mixer.

        pygame.mixer is SDL_mixer chunk playback: there is no supported API to push raw PCM
        into an already-playing stream without supplying a new ``Sound`` (each call wraps a
        ``Mix_Chunk``). True streaming would use ``SDL_OpenAudioDevice`` / a callback
        (e.g. PySDL2, ``sounddevice``) instead of mixer — a larger change than swapping this
        loop.
        """
        while self._running:
            if not self._pygame or not self._pygame.mixer.get_init():
                break

            if not self._channel:
                self._channel = self._pygame.mixer.find_channel(True)
                if not self._channel:
                    time.sleep(self._buffer_seconds)
                    continue

            if self._channel.get_queue() is not None:
                time.sleep(self._buffer_seconds / 2)
                continue

            pcm_bytes = self._render_buffer()
            sound = self._pygame.mixer.Sound(buffer=pcm_bytes)
            if not self._channel.get_busy():
                self._current_sound = sound
                self._channel.play(sound)
            else:
                self._queued_sound = sound
                self._channel.queue(sound)

    def _render_buffer(self) -> bytes:
        """Produce one mixer buffer of PCM.

        * **cpu_lockstep** (accurate): drain samples queued by ``tick_cpu_cycles``.
        * **Not lockstep** (fast VIC): advance reSID with ``resid_clock`` in one call
          (historical behaviour; matches pre-lockstep performance).
        """
        need = self._buffer_samples * 2
        if not self._cpu_lockstep and not self._rust_pcm_mode:
            chunk = self._render_buffer_decoupled()
        else:
            chunk = self._render_buffer_lockstep(need)
        self._wav_record_chunk(chunk)
        return chunk

    def _render_buffer_lockstep(self, need: int) -> bytes:
        """Drain ``_pcm_pending`` or pad with silence (lockstep / Rust PCM queue)."""
        with self._lock:
            if not self._sid_ptr:
                return bytes(need)
            if len(self._pcm_pending) >= need:
                chunk = bytes(self._pcm_pending[:need])
                del self._pcm_pending[:need]
                return chunk
            chunk = bytes(self._pcm_pending)
            self._pcm_pending.clear()
        pad = need - len(chunk)
        if pad > 0:
            self._note_lockstep_underrun(pad // 2)
            chunk += bytes(pad)
        return chunk

    def _render_buffer_decoupled(self) -> bytes:
        """Advance reSID by ~one buffer of C64 clocks (audio thread only)."""
        delta_cycles = int(self._cycles_per_buffer) + self._cycle_remainder
        if delta_cycles < 1:
            return bytes(self._buffer_samples * 2)

        delta_t = ctypes.c_int(delta_cycles)

        with self._lock:
            if not self._sid_ptr:
                return bytes(self._buffer_samples * 2)
            n = self._lib.resid_clock(
                self._sid_ptr,
                ctypes.byref(delta_t),
                self._pcm_buf,
                self._buffer_samples,
            )
        self._cycle_remainder = max(0, int(delta_t.value))

        if n <= 0:
            return bytes(self._buffer_samples * 2)

        produced = ctypes.cast(
            self._pcm_buf,
            ctypes.POINTER(ctypes.c_int16 * n),
        )[0]
        raw = bytes(produced)
        if n < self._buffer_samples:
            raw += bytes((self._buffer_samples - n) * 2)
        return raw
