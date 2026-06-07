"""Timeline ("movie script") parsing + ffmpeg video recording for local captures.

This module is intentionally free of any emulator imports: it only deals with
parsing the manifest ``script`` field into a sorted list of timed events and with
driving ``ffmpeg`` to encode a video (raw RGB frames on stdin) plus optional SID
audio (a WAV produced from offline reSID PCM, muxed in afterwards).

The emulator-facing engine that *executes* these events lives in
``readme_screenshot_common.capture_entry``.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional


def _import_core(mod: str):
    """Import a c64py core module without requiring the package on PYTHONPATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    parent = os.path.dirname(repo)
    pkg = os.path.basename(repo)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module(f"{pkg}.{mod}")


# Canonical C64 matrix table + payload parsers live in the core package so the
# live emulator (TCP INJECT) and this capture tool stay in sync.
_km = _import_core("keyboard_matrix")
parse_matrix_strokes = _km.parse_matrix_strokes
parse_joy_mask = _km.parse_joy_mask

# PAL master clock (CPU/phi2 cycles per second) and cycles per video frame.
PAL_CLOCK_HZ = 985_248
NTSC_CLOCK_HZ = 1_022_727
PAL_CYCLES_PER_FRAME = 312 * 63  # 19656

_TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(c|s|ms|f)?\s*$", re.IGNORECASE)

# Default hold/gap for matrix key presses and joystick (relative to clock_hz).
DEFAULT_KEY_HOLD_MS = 80.0
DEFAULT_KEY_GAP_MS = 40.0
DEFAULT_JOY_HOLD_MS = 250.0


def parse_time(value: Any, *, clock_hz: int = PAL_CLOCK_HZ,
               cycles_per_frame: int = PAL_CYCLES_PER_FRAME) -> int:
    """Parse a timeline timestamp into emulator cycles.

    Accepts an int/float (interpreted as raw cycles) or a string with a unit
    suffix: ``c`` cycles, ``s`` seconds, ``ms`` milliseconds, ``f`` PAL frames.
    A bare numeric string (no suffix) is treated as cycles.
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise ValueError(f"invalid time value: {value!r}")
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if not isinstance(value, str):
        raise ValueError(f"invalid time value: {value!r}")
    m = _TIME_RE.match(value)
    if not m:
        raise ValueError(
            f"bad time {value!r}: use <n>c (cycles), <n>s, <n>ms, or <n>f (frames)"
        )
    num = float(m.group(1))
    unit = (m.group(2) or "c").lower()
    if unit == "c":
        return max(0, int(num))
    if unit == "s":
        return max(0, int(round(num * clock_hz)))
    if unit == "ms":
        return max(0, int(round(num / 1000.0 * clock_hz)))
    if unit == "f":
        return max(0, int(round(num * cycles_per_frame)))
    raise ValueError(f"unknown time unit in {value!r}")


@dataclass
class VideoSpec:
    """A recording window declared by a ``video`` timeline event."""

    output: str
    start_cycle: int
    end_cycle: int
    fps: int = 50
    audio: bool = True


@dataclass
class JoySpec:
    """A joystick press: ``mask`` of OR-combined direction/button bits on ``port``."""

    port: int
    mask: int
    label: str


@dataclass
class ScriptEvent:
    """One timed action on the capture timeline."""

    at_cycle: int
    kind: str  # "key" | "screenshot" | "video" | "joystick"
    key: Optional[str] = None
    screenshot: Optional[str] = None
    video: Optional[VideoSpec] = None
    # Key options.
    method: str = "buffer"  # "buffer" (KERNAL queue) or "matrix" (CIA1 scan)
    hold_cycles: int = 0
    gap_cycles: int = 0
    # Joystick options.
    joy: Optional[JoySpec] = None
    order: int = 0  # preserves manifest order for equal timestamps


def parse_matrix_keys(payload: str) -> List[Any]:
    """Expand a key payload into serial matrix keystrokes (core ``Stroke`` list).

    Thin wrapper over the shared core parser; unknown characters/tokens are
    reported as a warning and skipped.
    """
    strokes, unknown = parse_matrix_strokes(payload)
    if unknown:
        print(f"WARN: matrix keys: skipped unsupported {unknown}", file=sys.stderr)
    return strokes


def _parse_joy_mask(spec: str) -> Tuple[int, str]:
    """Parse ``up+fire`` style direction list into (mask, normalized label)."""
    return parse_joy_mask(spec)


def _parse_video_payload(
    payload: Any,
    *,
    at_cycle: int,
    clock_hz: int,
    cycles_per_frame: int,
) -> VideoSpec:
    """Parse a ``video`` action.

    String form ``"<duration>:<output>"`` (e.g. ``"30s:out.mp4"``) records
    ``duration`` starting at the event time. Object form supports
    ``{duration|to, output, fps, audio}`` where ``to`` is an absolute timestamp.
    """
    if isinstance(payload, str):
        if ":" not in payload:
            raise ValueError(
                f"video string must be '<duration>:<output>', got {payload!r}"
            )
        dur_s, out = payload.split(":", 1)
        dur = parse_time(dur_s, clock_hz=clock_hz, cycles_per_frame=cycles_per_frame)
        return VideoSpec(output=out.strip(), start_cycle=at_cycle,
                         end_cycle=at_cycle + dur)
    if isinstance(payload, Mapping):
        out = payload.get("output")
        if not out:
            raise ValueError("video event missing 'output'")
        if "to" in payload:
            end = parse_time(payload["to"], clock_hz=clock_hz,
                             cycles_per_frame=cycles_per_frame)
        elif "duration" in payload:
            dur = parse_time(payload["duration"], clock_hz=clock_hz,
                             cycles_per_frame=cycles_per_frame)
            end = at_cycle + dur
        else:
            raise ValueError("video event needs 'duration' or 'to'")
        if end <= at_cycle:
            raise ValueError("video window must end after it starts")
        return VideoSpec(
            output=str(out),
            start_cycle=at_cycle,
            end_cycle=end,
            fps=int(payload.get("fps", 50)),
            audio=bool(payload.get("audio", True)),
        )
    raise ValueError(f"invalid video payload: {payload!r}")


def parse_script(
    script: Any,
    *,
    clock_hz: int = PAL_CLOCK_HZ,
    cycles_per_frame: int = PAL_CYCLES_PER_FRAME,
) -> List[ScriptEvent]:
    """Parse the manifest ``script`` list into sorted :class:`ScriptEvent`s."""
    if not isinstance(script, list):
        raise ValueError("'script' must be a list of timeline events")
    events: List[ScriptEvent] = []
    for i, item in enumerate(script):
        if not isinstance(item, Mapping):
            raise ValueError(f"script[{i}] must be an object")
        at_raw = item.get("at", item.get("cycle", 0))
        at_cycle = parse_time(at_raw, clock_hz=clock_hz,
                              cycles_per_frame=cycles_per_frame)

        def _t(key: str, default_ms: float) -> int:
            if key in item:
                return parse_time(item[key], clock_hz=clock_hz,
                                  cycles_per_frame=cycles_per_frame)
            return int(round(default_ms / 1000.0 * clock_hz))

        if "key" in item or "keys" in item:
            payload = item.get("key", item.get("keys"))
            method = str(item.get("method", "buffer")).lower()
            if method not in ("buffer", "matrix"):
                raise ValueError(f"key method must be 'buffer' or 'matrix', got {method!r}")
            events.append(ScriptEvent(
                at_cycle, "key", key=str(payload), method=method,
                hold_cycles=_t("hold", DEFAULT_KEY_HOLD_MS),
                gap_cycles=_t("gap", DEFAULT_KEY_GAP_MS), order=i,
            ))
        elif "joystick" in item or "joy" in item:
            payload = item.get("joystick", item.get("joy"))
            if isinstance(payload, str):
                # "<port>:<dirs>[:<hold>]" e.g. "2:up+fire:200ms"
                bits = payload.split(":")
                if len(bits) < 2:
                    raise ValueError(f"joy string must be '<port>:<dirs>[:<hold>]', got {payload!r}")
                port = int(bits[0])
                mask, label = _parse_joy_mask(bits[1])
                hold = (parse_time(bits[2], clock_hz=clock_hz, cycles_per_frame=cycles_per_frame)
                        if len(bits) >= 3 and bits[2].strip()
                        else int(round(DEFAULT_JOY_HOLD_MS / 1000.0 * clock_hz)))
            elif isinstance(payload, Mapping):
                port = int(payload.get("port", 2))
                mask, label = _parse_joy_mask(str(payload.get("press", payload.get("dir", ""))))
                # 'hold' lives inside the joystick payload here, not on the event.
                if "hold" in payload:
                    hold = parse_time(payload["hold"], clock_hz=clock_hz,
                                      cycles_per_frame=cycles_per_frame)
                else:
                    hold = int(round(DEFAULT_JOY_HOLD_MS / 1000.0 * clock_hz))
            else:
                raise ValueError(f"invalid joystick payload: {payload!r}")
            if port not in (1, 2):
                raise ValueError(f"joystick port must be 1 or 2, got {port}")
            events.append(ScriptEvent(
                at_cycle, "joystick", joy=JoySpec(port=port, mask=mask, label=label),
                hold_cycles=hold, order=i,
            ))
        elif "screenshot" in item or "take_screenshot" in item:
            out = item.get("screenshot", item.get("take_screenshot"))
            events.append(ScriptEvent(at_cycle, "screenshot",
                                      screenshot=str(out), order=i))
        elif "video" in item or "take_video" in item:
            payload = item.get("video", item.get("take_video"))
            spec = _parse_video_payload(
                payload, at_cycle=at_cycle, clock_hz=clock_hz,
                cycles_per_frame=cycles_per_frame,
            )
            events.append(ScriptEvent(at_cycle, "video", video=spec, order=i))
        else:
            raise ValueError(
                f"script[{i}] has no recognized action "
                "(key/screenshot/video)"
            )
    events.sort(key=lambda e: (e.at_cycle, e.order))
    return events


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class VideoRecorder:
    """Encode raw rgb24 frames (via ffmpeg stdin) plus optional muxed SID audio.

    Frames are piped to an ``ffmpeg`` process that writes a video-only temp file;
    SID PCM (mono int16 at ``sample_rate``) is buffered and, on :meth:`stop`,
    written to a WAV and muxed with the video into the final ``output``.
    """

    def __init__(
        self,
        spec: VideoSpec,
        *,
        sample_rate: int = 44_100,
    ) -> None:
        self.spec = spec
        self.sample_rate = sample_rate
        self._proc: Optional[subprocess.Popen] = None
        self._tmp_video: Optional[Path] = None
        self._wav_buf = bytearray()
        self._width = 0
        self._height = 0
        self._frames = 0
        self._ffmpeg = shutil.which("ffmpeg")
        if self._ffmpeg is None:
            raise RuntimeError(
                "ffmpeg not found on PATH; install it (e.g. `brew install ffmpeg`)"
            )

    @property
    def with_audio(self) -> bool:
        return bool(self.spec.audio)

    def start(self, width: int, height: int) -> None:
        self._width, self._height = width, height
        out = Path(self.spec.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".video.mp4", dir=str(out.parent))
        import os
        os.close(fd)
        self._tmp_video = Path(tmp)
        cmd = [
            self._ffmpeg, "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}", "-r", str(self.spec.fps),
            "-i", "-",
            # Pad odd dimensions to even (yuv420p / libx264 requirement).
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=neighbor",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-preset", "medium",
            str(self._tmp_video),
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def write_frame(self, rgb_bytes: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("recorder not started")
        self._proc.stdin.write(rgb_bytes)
        self._frames += 1

    def write_audio(self, pcm_bytes: bytes) -> None:
        if self.with_audio and pcm_bytes:
            self._wav_buf.extend(pcm_bytes)

    def stop(self) -> Path:
        """Finalize: close video pipe, write WAV, mux, and return output path."""
        out = Path(self.spec.output)
        if self._proc is not None:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.wait()
        assert self._tmp_video is not None

        if not self.with_audio or not self._wav_buf:
            self._tmp_video.replace(out)
            return out

        wav_path = self._tmp_video.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(bytes(self._wav_buf))

        mux = [
            self._ffmpeg, "-y",
            "-i", str(self._tmp_video),
            "-i", str(wav_path),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(out),
        ]
        rc = subprocess.run(
            mux, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        if rc != 0:
            print(
                f"WARN: ffmpeg mux failed (rc={rc}); keeping silent video {out}",
                file=sys.stderr,
            )
            self._tmp_video.replace(out)
        else:
            self._tmp_video.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
        return out
