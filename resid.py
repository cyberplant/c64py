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
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import sys
import threading
import time
import warnings
from typing import List, Optional


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

        # Fractional clock cycle accumulator: cycles owed but not yet fed
        # to reSID.  We accumulate CPU cycles here and drain in the audio
        # worker thread.
        self._cycle_acc: float = 0.0
        self._cycles_per_buffer = self._clock_hz * self._buffer_seconds

        # PCM output buffer (shared between audio worker and _render_buffer)
        self._pcm_buf = (ctypes.c_int16 * self._buffer_samples)()
        self._current_sound = None  # keep Sound alive while playing
        self._queued_sound = None   # keep queued Sound alive

        # pygame mixer
        self._pygame = None
        self._channel = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._init_audio(mixer_buffer)

    # ------------------------------------------------------------------
    # Clock / standard helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clock_for_standard(video_standard: str) -> int:
        return 985248 if video_standard == "pal" else 1022727

    def set_video_standard(self, video_standard: str) -> None:
        """Update the SID clock frequency for the given video standard."""
        self._clock_hz = self._clock_for_standard(video_standard)
        with self._lock:
            self._lib.resid_set_sampling_parameters(
                self._sid_ptr,
                float(self._clock_hz),
                self._sampling_method,
                float(self._sample_rate),
                -1.0,
            )

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
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._channel:
            try:
                self._channel.stop()
            except Exception:
                pass
        self._current_sound = None
        self._queued_sound = None
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
        # for SIGTERM/SIGINT/SIGQUIT/SIGHUP; those must only run on the main thread.
        _SIG_BLOCK = getattr(signal, 'SIG_BLOCK', None)
        _PYGAME_SIGS = {
            getattr(signal, s) for s in ('SIGTERM', 'SIGINT', 'SIGQUIT', 'SIGHUP')
            if hasattr(signal, s)
        }
        _old_mask = None
        if _SIG_BLOCK is not None and _PYGAME_SIGS:
            try:
                _old_mask = signal.pthread_sigmask(_SIG_BLOCK, _PYGAME_SIGS)
            except OSError:
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
        """Background thread: render reSID output and feed pygame mixer."""
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
        """Advance reSID by one buffer's worth of clock cycles and return PCM."""
        # Number of C64 clock cycles to emulate for this buffer
        delta_cycles = int(self._cycles_per_buffer)
        if delta_cycles < 1:
            # Extremely low sample rate – return silence
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

        if n <= 0:
            return bytes(self._buffer_samples * 2)

        # Cast the PCM buffer to an array of exactly `n` samples and convert
        # to bytes, zero-padding to fill the full buffer if reSID produced
        # fewer samples than requested (can happen at buffer boundaries).
        produced = ctypes.cast(
            self._pcm_buf,
            ctypes.POINTER(ctypes.c_int16 * n),
        )[0]
        raw = bytes(produced)
        if n < self._buffer_samples:
            raw += bytes((self._buffer_samples - n) * 2)
        return raw
