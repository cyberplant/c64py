"""
drives/c1541_emulator.py — Commodore 1541 disk drive emulation.

The 1541 is a full computer with:
- 6502 CPU @ 1MHz
- 2KB RAM
- 16KB ROM (DOS + Serial/GCR routines)
- VIA chips for I/O (6522)
- IEC serial bus interface

Run as a standalone TCP drive server:

    python -m c64py.drives.c1541_emulator --disk game.d64 --device 8 --port 6408

Or create a fresh blank image and serve it:

    python -m c64py.drives.c1541_emulator --new-disk ./blank.d64 --device 8 --port 6408

The server speaks a newline-delimited JSON protocol so multiple instances can
be chained, or an ESP32/microcontroller can implement the same interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Iterator, TYPE_CHECKING

from ..cpu import CPU6502
from ..cpu_state import CIATimer
from ..via6522 import VIA6522
from .drive import DiskDrive
from .iec_backend import IECDriveBackend

if TYPE_CHECKING:
    from ..iec_bus import IECBus
    from ..d64 import D64Image


@dataclass
class ChannelState:
    """Per-channel state on the 1541's IEC interface."""

    channel: int
    filename_buf: bytearray = field(default_factory=bytearray)
    is_open: bool = False
    byte_iter: Optional[Iterator[int]] = None
    pending: Optional[int] = None
    found: bool = True


class Drive1541Memory:
    """Memory map for 1541 drive (64KB address space).

    Layout (the 1541 is a 6502 + 2KB RAM + 16KB ROM + 2 VIAs):

        $0000-$07FF  RAM (mirrored every $0800 up to $17FF on real chip)
        $1800-$1BFF  VIA1 (IEC interface) — mirrored every 16 bytes
        $1C00-$1FFF  VIA2 (disk controller) — mirrored every 16 bytes
        $8000-$9FFF  Optional Serial / DOS half ROM
        $C000-$FFFF  DOS ROM (16KB)
    """

    def __init__(self):
        self.ram = bytearray(0x0800)
        self.rom_dos = None
        self.rom_serial = None

        self.via1 = VIA6522(name="via1")
        self.via2 = VIA6522(name="via2")

        self._on_zp_write: Optional[callable] = None

        self.sid = None
        self.kernal_rom = b"\x00"
        self.kernal_shortcuts_enabled = False
        self.video_standard = "pal"
        self.raster_line = 0
        self.raster_cycles = 0
        self.badline_cycles = 0
        self.vic_interrupt_state = 0
        self.vic_snapshot_each_emulated_frame = False
        self.cia1_timer_a = CIATimer()
        self.cia1_timer_b = CIATimer()
        self.cia1_icr = 0
        self.pending_irq = False

    def sid_tick_cpu_cycles(self, n: int) -> None:
        pass

    def recompute_pending_irq(self) -> None:
        self.pending_irq = self.via1.irq_pending or self.via2.irq_pending

    def beam_capture_raster_line(self, line: int) -> None:
        pass

    def snapshot_vic_render_state(self) -> None:
        pass

    def load_rom(self, dos_rom: bytes, serial_rom: Optional[bytes] = None) -> None:
        if len(dos_rom) != 16384:
            raise ValueError(f"DOS ROM must be 16KB, got {len(dos_rom)} bytes")
        self.rom_dos = bytes(dos_rom)
        if serial_rom is not None:
            if len(serial_rom) != 8192:
                raise ValueError(f"Serial ROM must be 8KB, got {len(serial_rom)} bytes")
            self.rom_serial = bytes(serial_rom)

    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x0800:
            return self.ram[addr]
        if addr < 0x1800:
            return self.ram[addr & 0x07FF]
        if addr < 0x1C00:
            return self.via1.read(addr & 0x0F)
        if addr < 0x2000:
            return self.via2.read(addr & 0x0F)
        if 0x8000 <= addr < 0xA000 and self.rom_serial is not None:
            return self.rom_serial[addr - 0x8000]
        if addr >= 0xC000 and self.rom_dos is not None:
            return self.rom_dos[addr - 0xC000]
        return 0xFF

    def write(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        value &= 0xFF
        if addr < 0x0800:
            self.ram[addr] = value
            if addr < 0x06 and self._on_zp_write is not None:
                self._on_zp_write(addr, value)
            return
        if addr < 0x1800:
            self.ram[addr & 0x07FF] = value
            return
        if addr < 0x1C00:
            self.via1.write(addr & 0x0F, value)
            return
        if addr < 0x2000:
            self.via2.write(addr & 0x0F, value)
            return


class Drive1541(IECDriveBackend):
    """Emulates a Commodore 1541 disk drive."""

    def __init__(self, device_number: int = 8):
        self.device_number = device_number

        self.memory = Drive1541Memory()
        self.cpu = CPU6502(self.memory)

        self.memory.via1._on_pb_write = self._on_via1_pb_write

        self.iec_bus: Optional[IECBus] = None

        self.disk: Optional[D64Image] = None
        self.disk_filename: Optional[str] = None

        self._disk_helper: DiskDrive = DiskDrive(device_number=device_number)

        self.listening = False
        self.talking = False
        self.current_channel: Optional[int] = None
        self._opening_channel: Optional[int] = None

        self.channels: dict[int, ChannelState] = {}
        self.command_buffer = bytearray()

        self._awake = True
        self._executing_pc: Optional[int] = None
        self._boot_done = True
        self._reset_done = False

        jumper_bits = ((device_number - 8) & 0x03) ^ 0x03
        self._jumper_pb45 = (jumper_bits & 0x03) << 4
        self._refresh_via1_inputs()

        self.memory._on_zp_write = self._on_job_queue_write

    def load_rom(self, dos_rom: bytes, serial_rom: Optional[bytes] = None) -> None:
        self.memory.load_rom(dos_rom, serial_rom)
        self.cpu.state.pc = self.memory.read(0xFFFC) | (self.memory.read(0xFFFD) << 8)

    # ------------------------------------------------------------------
    # IEC ↔ VIA1 glue
    # ------------------------------------------------------------------

    def _refresh_via1_inputs(self) -> None:
        bus = self.iec_bus
        tag = f"drive{self.device_number}"
        if bus is None:
            atn_high = True
            clk_peer_high = True
            data_peer_high = True
        else:
            atn_high = bus.atn
            clk_peer_high = not (bus.clk_pullers - {tag})
            data_peer_high = not (bus.data_pullers - {tag})

        pb = 0
        # PB0 DATA IN: 1 = released (high), 0 = asserted (low)
        if data_peer_high:
            pb |= 0x01
        # PB2 CLK IN
        if clk_peer_high:
            pb |= 0x04
        pb |= self._jumper_pb45
        # PB6 ATN IN — INVERTED via 7406: ATN asserted (bus low) → PB6 HIGH
        if not atn_high:
            pb |= 0x40
        # PB1, PB3 are drive outputs; PB7 is ATN-ACK output.
        # Input register read for these reflects what the drive drove,
        # handled by DDRB merging in VIA read().
        pb |= 0x02 | 0x08 | 0x80

        self.memory.via1.set_pb_in(pb)
        self.memory.via1.set_ca1(not atn_high)
        self._on_via1_pb_write(self.memory.via1.orb, self.memory.via1.ddrb)

    def _on_via1_pb_write(self, orb: int, ddrb: int) -> None:
        bus = self.iec_bus
        if bus is None:
            return
        tag = f"drive{self.device_number}"
        atn_asserted = (bus is not None) and not bus.atn
        atnacc_bit = bool((ddrb & 0x80) and (orb & 0x80))
        xor_pulls_data = atn_asserted and not atnacc_bit
        pb1_pulls_data = bool((ddrb & 0x02) and (orb & 0x02))
        if pb1_pulls_data or xor_pulls_data:
            bus.set_data(tag, False)
        else:
            bus.set_data(tag, True)
        if (ddrb & 0x08) and (orb & 0x08):
            bus.set_clk(tag, False)
        else:
            bus.set_clk(tag, True)

    def attach_disk(self, disk: D64Image, filename: str = "") -> None:
        self.disk = disk
        self.disk_filename = filename
        self._disk_helper.attach_disk(disk, filename)

    def detach_disk(self) -> None:
        self.disk = None
        self.disk_filename = None
        self._disk_helper.detach_disk()

    def has_disk(self) -> bool:
        return self.disk is not None

    @property
    def led_on(self) -> bool:
        via2 = self.memory.via2
        if not (via2.ddrb & 0x08):
            return False
        return bool(via2.orb & 0x08)

    # ------------------------------------------------------------------
    # Job-queue trap
    # ------------------------------------------------------------------

    _JOB_BUFFER_BASE = (0x0300, 0x0400, 0x0500, 0x0600, 0x0700, 0x0700)

    def _on_job_queue_write(self, addr: int, value: int) -> None:
        if value & 0x80 == 0:
            return
        exec_pc = self._executing_pc
        if exec_pc is not None and 0xEAA0 <= exec_pc <= 0xEAC0:
            return
        buf = addr & 0x07
        if buf > 5:
            return
        ts_base = 0x06 + buf * 2
        track = self.memory.ram[ts_base]
        sector = self.memory.ram[ts_base + 1]
        status = self._service_job(value & 0xF0, buf, track, sector)
        self.memory.ram[buf] = status & 0x0F
        self._awake = True

    def _service_job(self, opcode: int, buf: int, track: int, sector: int) -> int:
        if opcode in (0xB0, 0xC0, 0xA0):
            return 0x01
        if opcode in (0xD0, 0xE0):
            return 0x0F
        if self.disk is None:
            return 0x0F
        base = self._JOB_BUFFER_BASE[buf]
        if opcode == 0x80:
            try:
                data = self.disk.read_sector(track, sector)
            except (IndexError, ValueError, AttributeError):
                return 0x02
            if data is None or len(data) != 256:
                return 0x02
            for i, b in enumerate(data):
                self.memory.ram[base + i if base + i < 0x0800 else (base + i) & 0x07FF] = b
            return 0x01
        if opcode == 0x90:
            if self.disk is None:
                return 0x0F
            buffer = bytes(self.memory.ram[(base + i) & 0x07FF] for i in range(256))
            try:
                self.disk.write_sector(track, sector, buffer)
            except (IndexError, ValueError, AttributeError):
                return 0x08
            return 0x01
        return 0x0F

    def notify_bus_change(self) -> None:
        self._refresh_via1_inputs()
        self._awake = True

    def step(self, cycles: int = 1) -> int:
        if self.memory.rom_dos is None or cycles <= 0:
            return 0

        consumed = 0
        budget = max(1, int(cycles))
        while consumed < budget:
            self._executing_pc = self.cpu.state.pc
            used = self.cpu.step()
            if used <= 0:
                used = 1
            self.memory.via1.tick(used)
            self.memory.via2.tick(used)
            self.memory.recompute_pending_irq()
            if self.memory.pending_irq and not (self.cpu.state.p & 0x04):
                self.cpu._handle_irq()
            if (self.memory.ram[0x7C] != 0
                    and 0xEC07 <= self.cpu.state.pc <= 0xEC9B):
                self.cpu.state.pc = 0xEC00
            consumed += used
        self._awake = True
        return consumed

    def _is_idle(self) -> bool:
        if self.memory.via1.t1_active or self.memory.via2.t1_active:
            return False
        if self.memory.via1.t2_active or self.memory.via2.t2_active:
            return False
        if self.memory.pending_irq:
            return False
        bus = self.iec_bus
        if bus is None:
            return True
        return bus.atn

    def reset(self) -> None:
        for i in range(len(self.memory.ram)):
            self.memory.ram[i] = 0
        self.memory.via1.reset()
        self.memory.via2.reset()
        self.memory.via1._on_pb_write = self._on_via1_pb_write
        bus = self.iec_bus
        if bus is not None:
            tag = f"drive{self.device_number}"
            bus.set_clk(tag, True)
            bus.set_data(tag, True)
        if self.memory.rom_dos is not None:
            self.cpu.state.pc = self.memory.read(0xFFFC) | (self.memory.read(0xFFFD) << 8)
        self.cpu.state.sp = 0xFD
        self.cpu.state.p |= 0x04
        self._reset_done = True
        self.channels.clear()
        self.command_buffer.clear()
        self._opening_channel = None
        self.listening = False
        self.talking = False
        self.current_channel = None
        self._awake = True
        self._refresh_via1_inputs()

    # ------------------------------------------------------------------
    # IEC bus event handlers
    # ------------------------------------------------------------------

    def on_atn_changed(self, atn_state: bool) -> None:
        pass

    def on_listen(self) -> None:
        self.listening = True
        self.talking = False

    def on_unlisten(self) -> None:
        self.listening = False

    def on_talk(self) -> None:
        self.talking = True
        self.listening = False

    def on_untalk(self) -> None:
        self.talking = False

    def on_secondary_address(self, channel: int) -> None:
        self.current_channel = channel

    def iec_open_channel(self, channel: int) -> None:
        self._opening_channel = channel
        self.channels[channel] = ChannelState(channel=channel)

    def iec_close_channel(self, channel: int) -> None:
        self.channels.pop(channel, None)
        if self._opening_channel == channel:
            self._opening_channel = None

    def iec_secondary(self, channel: int, kind: str) -> None:
        self.current_channel = channel
        if self._opening_channel is not None and self._opening_channel != channel:
            self._opening_channel = None

    def iec_unlisten(self) -> None:
        ch = self._opening_channel
        self._opening_channel = None
        if ch is None:
            return
        state = self.channels.get(ch)
        if state is None:
            return
        try:
            filename = state.filename_buf.decode("ascii", errors="replace")
        except Exception:
            filename = ""
        data = self._disk_helper.load_file(filename, secondary_address=ch)
        if data is None:
            state.found = False
            state.byte_iter = iter(())
            state.is_open = True
            return
        state.found = True
        state.byte_iter = iter(data)
        state.is_open = True

    def iec_untalk(self) -> None:
        pass

    def iec_receive_byte(self, byte: int, eoi: bool = False) -> None:
        byte &= 0xFF
        if self._opening_channel is not None:
            state = self.channels.setdefault(
                self._opening_channel, ChannelState(channel=self._opening_channel)
            )
            state.filename_buf.append(byte)
            return
        ch = self.current_channel
        if ch == 15:
            self.command_buffer.append(byte)
            return

    def iec_send_byte(self):
        ch = self.current_channel
        if ch is None:
            return None
        state = self.channels.get(ch)
        if state is None or state.byte_iter is None:
            return None
        if not state.found:
            if state.pending is None and not getattr(state, "_fnf_sent", False):
                state._fnf_sent = True  # type: ignore[attr-defined]
                return (0x00, True)
            return None
        if state.pending is None:
            try:
                state.pending = next(state.byte_iter)
            except StopIteration:
                return None
        current = state.pending
        try:
            state.pending = next(state.byte_iter)
            return (current & 0xFF, False)
        except StopIteration:
            state.pending = None
            return (current & 0xFF, True)

    def receive_byte(self, byte: int) -> None:
        """Legacy hook: forwards to :meth:`iec_receive_byte`."""
        self.iec_receive_byte(byte, eoi=False)

    def send_byte(self) -> Optional[int]:
        """Legacy hook: returns just the byte (no EOI)."""
        result = self.iec_send_byte()
        if result is None:
            return None
        return result[0]


# ---------------------------------------------------------------------------
# Standalone TCP drive server
# ---------------------------------------------------------------------------

def _run_server(device: int, port: int, disk_path: Optional[str],
                dos_rom_path: Optional[str], serial_rom_path: Optional[str],
                interface: str = "headless",
                emulation: str = "fast",
                log_level: int = logging.INFO) -> None:
    """Run an asyncio TCP server wrapping a Drive1541 instance.

    ``log_level`` is a :mod:`logging` numeric level (e.g. ``logging.DEBUG``).
    """
    import asyncio
    import json
    import sys

    import collections

    logging.basicConfig(
        level=log_level,
        format=f"%(asctime)s [drive{device}] %(levelname)s %(message)s",
    )
    log = logging.getLogger(f"c1541.drive{device}")
    log.setLevel(log_level)

    # Shared ring buffer: every handler appends (timestamp_str, level, message).
    # The TUI polls this; headless mode drains it to nothing (stdout via basicConfig).
    log_ring: collections.deque = collections.deque(maxlen=200)

    class _RingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            ts = self.formatter.formatTime(record, "%H:%M:%S") if self.formatter else ""
            log_ring.append((ts, record.levelname, record.getMessage()))

    _rh = _RingHandler()
    _rh.setFormatter(logging.Formatter())
    log.addHandler(_rh)

    log.info("interface=%s emulation=%s", interface, emulation)

    if emulation == "accurate-rust":
        log.warning("accurate-rust drive port WIP; falling back to accurate-python")
    if emulation in ("accurate-python", "accurate-rust"):
        log.warning(
            "accurate-python GCR/IEC path WIP; behaving like fast until M2 lands"
        )

    if interface == "graphics":
        log.error("--interface graphics is not implemented yet; see TODO.md")
        sys.exit(2)

    # Build the drive
    drive = Drive1541(device_number=device)

    if dos_rom_path:
        with open(dos_rom_path, "rb") as fh:
            dos_rom = fh.read()
        serial_rom = None
        if serial_rom_path:
            with open(serial_rom_path, "rb") as fh:
                serial_rom = fh.read()
        drive.load_rom(dos_rom, serial_rom)
        drive.reset()
        log.info("ROM loaded from %s", dos_rom_path)
    else:
        log.info("No ROM provided — byte-level protocol only (no VIA/CPU emulation)")

    if disk_path:
        from ..d64 import load_d64
        disk = load_d64(disk_path)
        drive.attach_disk(disk, disk_path)
        _dname, _did = disk.read_bam()
        log.info("Disk inserted: '%s' (id: %s) — %s",
                 _dname.strip(), _did.strip(), disk_path)

    async def handle_client(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("C64 emulator connected from %s", peer)

        # Send greeting
        writer.write(json.dumps({"type": "ready", "device": device}).encode() + b"\n")
        await writer.drain()

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    log.warning("Bad JSON: %r", line)
                    continue

                mtype = msg.get("type")
                reply = None

                if mtype == "listen":
                    drive.on_listen()
                    ch = msg.get("secondary")
                    if ch is not None:
                        drive.on_secondary_address(int(ch))
                    log.debug("IEC LISTEN secondary=%s", ch)

                elif mtype == "talk":
                    drive.on_talk()
                    ch = msg.get("secondary")
                    if ch is not None:
                        drive.on_secondary_address(int(ch))
                    log.debug("IEC TALK secondary=%s", ch)

                elif mtype == "unlisten":
                    drive.iec_unlisten()
                    drive.on_unlisten()
                    log.debug("IEC UNLISTEN")

                elif mtype == "untalk":
                    drive.iec_untalk()
                    drive.on_untalk()
                    log.debug("IEC UNTALK")

                elif mtype == "open_channel":
                    drive.iec_open_channel(int(msg["channel"]))
                    log.debug("Channel %d opened", int(msg["channel"]))

                elif mtype == "close_channel":
                    drive.iec_close_channel(int(msg["channel"]))
                    log.debug("Channel %d closed", int(msg["channel"]))

                elif mtype == "secondary":
                    drive.iec_secondary(int(msg["channel"]), msg.get("kind", "data"))
                    drive.on_secondary_address(int(msg["channel"]))

                elif mtype == "send_byte":
                    drive.iec_receive_byte(int(msg["byte"]), bool(msg.get("eoi", False)))

                elif mtype == "request_byte":
                    result = drive.iec_send_byte()
                    if result is None:
                        reply = {"type": "no_data"}
                    else:
                        byte, eoi = result
                        reply = {"type": "byte", "byte": byte, "eoi": eoi}

                elif mtype == "fast_load":
                    import base64
                    filename = msg.get("filename", "")
                    secondary = int(msg.get("secondary", 0))
                    disp_name = filename if filename else "$"
                    log.info("LOAD %r requested (secondary=%d)", disp_name, secondary)
                    helper = drive._disk_helper
                    data = helper.load_file(filename, secondary)
                    if data is None:
                        err = helper.last_error
                        code = err[0] if err[0] else 62
                        message = err[1] if err[0] else "FILE NOT FOUND"
                        log.warning("LOAD %r failed: %d %s", disp_name, code, message)
                        reply = {"type": "fast_load_reply", "ok": False,
                                 "error_code": code, "error_message": message}
                    else:
                        load_addr = (data[0] | (data[1] << 8)) if len(data) >= 2 else 0x0801
                        log.info("LOAD %r OK — %d bytes, load addr $%04X",
                                 disp_name, len(data), load_addr)
                        reply = {"type": "fast_load_reply", "ok": True,
                                 "data": base64.b64encode(bytes(data)).decode("ascii"),
                                 "load_addr": load_addr}
                        if helper.last_loaded_dos_filetype is not None:
                            reply["dos_filetype"] = helper.last_loaded_dos_filetype

                elif mtype == "fast_save":
                    import base64
                    filename = msg.get("filename", "")
                    try:
                        raw = base64.b64decode(msg.get("data", ""))
                    except Exception:
                        raw = b""
                    log.info("SAVE %r requested (%d bytes)", filename, len(raw))
                    helper = drive._disk_helper
                    ok = helper.save_file(filename, raw)
                    if ok:
                        log.info("SAVE %r OK — %d bytes written to disk",
                                 filename, len(raw))
                        reply = {"type": "fast_save_reply", "ok": True}
                    else:
                        code, message, _, _ = helper.last_error
                        log.warning("SAVE %r failed: %d %s", filename, code, message)
                        reply = {"type": "fast_save_reply", "ok": False,
                                 "error_code": code, "error_message": message}

                elif mtype == "attach_disk":
                    from ..d64 import load_d64
                    path = msg.get("path", "")
                    log.info("Attaching disk image: %s", path)
                    try:
                        disk = load_d64(path)
                        drive.attach_disk(disk, path)
                        disk_name, disk_id = disk.read_bam()
                        log.info("Disk ready — name: '%s'  id: '%s'",
                                 disk_name.strip(), disk_id.strip())
                        reply = {"type": "attach_disk_reply", "ok": True,
                                 "disk_name": disk_name, "disk_id": disk_id}
                    except Exception as exc:
                        log.error("attach_disk failed: %s", exc)
                        reply = {"type": "attach_disk_reply", "ok": False,
                                 "error": str(exc)}

                elif mtype == "detach_disk":
                    old = drive.disk_filename or "(none)"
                    drive.detach_disk()
                    reply = {"type": "detach_disk_reply", "ok": True}
                    log.info("Disk ejected: %s", old)

                elif mtype == "status":
                    helper = drive._disk_helper
                    reply = {"type": "status_reply",
                             "led_on": bool(getattr(drive, "led_on", False)),
                             "disk": drive.disk_filename or "",
                             "status": helper.get_status(),
                             "implementation": "c64py-c1541-emulator",
                             "media": "d64"}

                else:
                    log.debug("Unknown message type: %s", mtype)

                if reply is not None:
                    writer.write(json.dumps(reply).encode() + b"\n")
                    await writer.drain()

        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            log.info("C64 emulator disconnected (%s)", peer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def main() -> None:
        server = await asyncio.start_server(handle_client, "0.0.0.0", port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("Drive %d listening on %s", device, addrs)
        async with server:
            await server.serve_forever()

    if interface == "text":
        import threading
        from .text_ui import DriveTextualApp

        def _run_async() -> None:
            try:
                asyncio.run(main())
            except Exception as exc:  # noqa: BLE001
                log.error("server loop died: %s", exc)

        t = threading.Thread(target=_run_async, daemon=True)
        t.start()

        if not sys.stdout.isatty():
            log.error("--interface text requires a TTY; falling back to headless")
            t.join()
            return

        # Remove all stdout/stderr handlers from the root logger so Textual's
        # terminal output is not polluted by logging lines written to stdout.
        root_log = logging.getLogger()
        for _h in root_log.handlers[:]:
            if hasattr(_h, "stream") and _h.stream in (sys.stdout, sys.stderr):
                root_log.removeHandler(_h)
        # Also silence the drive-specific logger's own handlers (keeps ring buffer).
        for _h in log.handlers[:]:
            if hasattr(_h, "stream") and _h.stream in (sys.stdout, sys.stderr):
                log.removeHandler(_h)

        app = DriveTextualApp(drive=drive, device=device, port=port, log_ring=log_ring)
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        return

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped.")
        sys.exit(0)


if __name__ == "__main__":
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        description="Standalone Commodore 1541 TCP drive server"
    )
    parser.add_argument(
        "--disk",
        metavar="D64",
        help="Path to an existing D64 disk image (file must already exist).",
    )
    parser.add_argument(
        "--new-disk",
        metavar="PATH",
        help=(
            "Create a new blank .d64 at PATH and attach it. The file must not exist "
            "(parent directories are created). Mutually exclusive with --disk."
        ),
    )
    parser.add_argument("--device", type=int, default=8, metavar="N",
                        help="IEC device number (default: 8)")
    parser.add_argument("--port", type=int, default=6400, metavar="PORT",
                        help="TCP port to listen on (default: 6400)")
    parser.add_argument("--dos-rom", metavar="ROM",
                        help="Path to 1541 DOS ROM (16KB binary)")
    parser.add_argument("--serial-rom", metavar="ROM",
                        help="Path to 1541 serial ROM (8KB binary, optional)")
    parser.add_argument(
        "--interface",
        choices=("headless", "text", "graphics"),
        default="headless",
        help="UI mode (default: headless)",
    )
    parser.add_argument(
        "--emulation",
        choices=("fast", "accurate-python", "accurate-rust"),
        default="fast",
        help="Drive emulation tier (default: fast)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        help=(
            "Python logging level for this process (DEBUG, INFO, WARNING, ERROR, …). "
            "Use DEBUG to see IEC wire JSON handlers (listen, open_channel, send_byte, …)."
        ),
    )
    args = parser.parse_args()

    _lvl_name = str(args.log_level).strip().upper()
    _log_level = getattr(logging, _lvl_name, None)
    if not isinstance(_log_level, int):
        print(
            f"ERROR: invalid --log-level {args.log_level!r} (try DEBUG, INFO, WARNING, ERROR)",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.disk and args.new_disk:
        print("ERROR: use only one of --disk or --new-disk", file=sys.stderr)
        sys.exit(2)

    disk_path: Optional[str] = None
    if args.new_disk:
        new_abs = os.path.normpath(os.path.abspath(os.path.expanduser(str(args.new_disk))))
        if not new_abs.lower().endswith(".d64"):
            print("ERROR: --new-disk path must end with .d64", file=sys.stderr)
            sys.exit(2)
        if os.path.lexists(new_abs):
            print(
                f"ERROR: --new-disk refuses to overwrite an existing file: {new_abs}",
                file=sys.stderr,
            )
            sys.exit(2)
        parent = os.path.dirname(new_abs)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            from ..d64 import create_blank_d64
        except ImportError:
            from c64py.d64 import create_blank_d64
        stem = os.path.splitext(os.path.basename(new_abs))[0].upper().replace(" ", "")[:16]
        if not stem:
            stem = "NEWDISK"
        create_blank_d64(stem, "64").save_to_file(new_abs)
        disk_path = new_abs
    elif args.disk:
        disk_abs = os.path.normpath(os.path.abspath(os.path.expanduser(str(args.disk))))
        if not os.path.isfile(disk_abs):
            print(f"ERROR: --disk file not found: {disk_abs}", file=sys.stderr)
            sys.exit(2)
        disk_path = disk_abs

    _run_server(
        device=args.device,
        port=args.port,
        disk_path=disk_path,
        dos_rom_path=args.dos_rom,
        serial_rom_path=args.serial_rom,
        interface=args.interface,
        emulation=args.emulation,
        log_level=_log_level,
    )
