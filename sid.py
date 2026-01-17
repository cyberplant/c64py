"""
Simplified SID audio emulation with pygame output.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional


class SidEmulator:
    """Minimal SID register handling with streaming audio output."""

    REG_COUNT = 0x20
    VOICE_COUNT = 3
    VOICE_STRIDE = 7
    VOLUME_REG = 0x18

    def __init__(
        self,
        *,
        video_standard: str = "pal",
        sample_rate: int = 44100,
        buffer_ms: int = 50,
        mixer_buffer: int = 512,
    ) -> None:
        self._registers = bytearray(self.REG_COUNT)
        self._lock = threading.Lock()
        self._sample_rate = int(sample_rate)
        self._buffer_samples = max(64, int(self._sample_rate * buffer_ms / 1000))
        self._buffer_seconds = self._buffer_samples / self._sample_rate
        self._clock_hz = self._clock_for_standard(video_standard)
        self._phase: List[float] = [0.0, 0.0, 0.0]
        self._noise_state: List[int] = [0x7FFFFF, 0x5AAAAA, 0x33CCCC]

        self._pygame = None
        self._channel = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._init_audio(mixer_buffer)

    @staticmethod
    def _clock_for_standard(video_standard: str) -> int:
        return 1022727 if video_standard == "pal" else 985248

    def set_video_standard(self, video_standard: str) -> None:
        self._clock_hz = self._clock_for_standard(video_standard)

    def read_register(self, offset: int) -> int:
        if not 0 <= offset < self.REG_COUNT:
            return 0
        with self._lock:
            return self._registers[offset]

    def write_register(self, offset: int, value: int) -> None:
        if not 0 <= offset < self.REG_COUNT:
            return
        with self._lock:
            self._registers[offset] = value & 0xFF

    def close(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._pygame and self._pygame.mixer.get_init():
            try:
                self._pygame.mixer.stop()
                self._pygame.mixer.quit()
            except Exception:
                pass

    def _init_audio(self, mixer_buffer: int) -> None:
        try:
            import pygame
        except ImportError as exc:
            raise RuntimeError("pygame is required for SID audio output") from exc

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
        self._thread = threading.Thread(target=self._audio_worker, daemon=True)
        self._thread.start()

    def _audio_worker(self) -> None:
        while self._running:
            if not self._pygame or not self._pygame.mixer.get_init():
                break

            if not self._has_active_output():
                if self._channel and self._channel.get_busy():
                    self._channel.stop()
                time.sleep(self._buffer_seconds)
                continue

            if not self._channel:
                self._channel = self._pygame.mixer.find_channel(True)
                if not self._channel:
                    time.sleep(self._buffer_seconds)
                    continue

            if self._channel.get_queue() is not None:
                time.sleep(self._buffer_seconds / 2)
                continue

            buffer = self._render_buffer()
            sound = self._pygame.mixer.Sound(buffer=buffer)
            if not self._channel.get_busy():
                self._channel.play(sound)
            else:
                self._channel.queue(sound)

    def _has_active_output(self) -> bool:
        regs = self._snapshot_registers()
        volume = regs[self.VOLUME_REG] & 0x0F
        if volume == 0:
            return False
        for voice in range(self.VOICE_COUNT):
            base = voice * self.VOICE_STRIDE
            control = regs[base + 4]
            if not (control & 0x01):
                continue
            if not (control & 0xF0):
                continue
            freq = regs[base] | (regs[base + 1] << 8)
            if freq == 0:
                continue
            return True
        return False

    def _snapshot_registers(self) -> bytes:
        with self._lock:
            return bytes(self._registers)

    def _render_buffer(self) -> bytes:
        regs = self._snapshot_registers()
        volume = (regs[self.VOLUME_REG] & 0x0F) / 15.0
        if volume == 0:
            return bytes(self._buffer_samples * 2)

        voices = []
        for voice in range(self.VOICE_COUNT):
            base = voice * self.VOICE_STRIDE
            control = regs[base + 4]
            if not (control & 0x01):
                continue

            waveform = self._select_waveform(control)
            if waveform is None:
                continue

            freq_reg = regs[base] | (regs[base + 1] << 8)
            if freq_reg == 0:
                continue
            freq_hz = freq_reg * self._clock_hz / 65536.0
            pulse_width = ((regs[base + 3] & 0x0F) << 8) | regs[base + 2]
            duty = max(0.05, min(0.95, pulse_width / 4096.0))
            voices.append((voice, waveform, freq_hz, duty))

        if not voices:
            return bytes(self._buffer_samples * 2)

        voice_count = len(voices)
        mix_scale = 0.8 / voice_count

        phases = self._phase[:]
        noises = self._noise_state[:]
        increments = [0.0, 0.0, 0.0]
        waveforms = [None, None, None]
        duties = [0.5, 0.5, 0.5]
        active = [False, False, False]

        for voice, waveform, freq_hz, duty in voices:
            active[voice] = True
            waveforms[voice] = waveform
            increments[voice] = freq_hz / self._sample_rate
            duties[voice] = duty

        output = bytearray(self._buffer_samples * 2)
        for i in range(self._buffer_samples):
            mix = 0.0
            for voice in range(self.VOICE_COUNT):
                if not active[voice]:
                    continue
                phase = phases[voice]
                waveform = waveforms[voice]
                if waveform == "saw":
                    sample = (phase * 2.0) - 1.0
                elif waveform == "triangle":
                    sample = (4.0 * phase - 1.0) if phase < 0.5 else (3.0 - 4.0 * phase)
                elif waveform == "pulse":
                    sample = 1.0 if phase < duties[voice] else -1.0
                else:
                    noises[voice] = self._advance_noise(noises[voice])
                    sample = 1.0 if (noises[voice] & 1) else -1.0

                mix += sample
                phase += increments[voice]
                if phase >= 1.0:
                    phase -= 1.0
                phases[voice] = phase

            sample_value = mix * mix_scale * volume
            if sample_value > 1.0:
                sample_value = 1.0
            elif sample_value < -1.0:
                sample_value = -1.0
            int_sample = int(sample_value * 32767)
            offset = i * 2
            output[offset:offset + 2] = int_sample.to_bytes(2, "little", signed=True)

        self._phase = phases
        self._noise_state = noises
        return bytes(output)

    @staticmethod
    def _advance_noise(state: int) -> int:
        feedback = ((state >> 22) ^ (state >> 17)) & 0x01
        return ((state << 1) | feedback) & 0x7FFFFF

    @staticmethod
    def _select_waveform(control: int) -> Optional[str]:
        if control & 0x20:
            return "saw"
        if control & 0x10:
            return "triangle"
        if control & 0x40:
            return "pulse"
        if control & 0x80:
            return "noise"
        return None
