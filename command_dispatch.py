"""Shared text command dispatcher used by the TCP/UDP server and the host
memory command channel.

The grammar is the existing TCP server grammar (see server.py docstrings and
README "Server Mode Commands"). Keeping the dispatcher in a standalone
function lets non-TCP transports (host memory mailbox, in-process tests)
reuse the same command surface without spinning up an EmulatorServer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import KEYBOARD_BUFFER_BASE, KEYBOARD_BUFFER_LEN_ADDR

if TYPE_CHECKING:
    from .emulator import C64


HELP_TEXT = """C64 Emulator TCP Server Commands:
STATUS              - Get current CPU state (PC, A, X, Y, SP, P, CYCLES)
SYS <address>       - Jump PC to address and continue execution (hex, e.g. $0400 or 0400)
MEMORY <address>    - Read memory at address (hex, e.g. $0400 or 0400)
WRITE <addr> <val>  - Write value to memory address (hex)
DUMP [start] [end]  - Dump memory range as hex (default: $0000-$FFFF)
SCREEN              - Get current screen contents (plain text)
SEND_KEY <code>     - Inject PETSCII key code (hex or decimal)
SEND_KEYS <codes..> - Inject multiple PETSCII key codes
SHOW_KEYBOARD_BUFFER- Show keyboard buffer length and contents
SHOW_CURRENT_LINE   - Show current screen line at cursor
LOAD <file>         - Load PRG file
ATTACH-DISK <file> [device] - Attach D64 disk image (device 8-11, default: 8)
DETACH-DISKS        - Detach all disk images
STOP                - Stop emulator execution
QUIT/EXIT           - Quit server and emulator
HELP/?              - Show this help message"""


def _parse_keycode(raw: str) -> int:
    cleaned = raw.strip()
    if cleaned.startswith('$') or cleaned.lower().startswith('0x'):
        return int(cleaned.replace('$', '').replace('0x', ''), 16)
    if any(c in 'ABCDEFabcdef' for c in cleaned):
        return int(cleaned, 16)
    return int(cleaned, 10)


def dispatch_text_command(emu: "C64", command: str) -> str:
    """Dispatch a single text command against ``emu`` and return the reply.

    QUIT/EXIT sets ``emu.running = False`` (the caller is responsible for
    tearing down its own server loop, e.g. EmulatorServer.running).
    Errors are returned as ``"ERROR: ..."`` strings rather than raised so
    every transport can ship them verbatim back to the client.
    """
    parts = command.split()
    if not parts:
        return "OK"

    cmd = parts[0].upper()

    if cmd == "HELP" or cmd == "?":
        return HELP_TEXT

    elif cmd == "STATUS":
        state = emu.get_cpu_state()
        cycles = getattr(emu, 'current_cycles', None)
        if cycles is None:
            cycles = state['cycles']
        return (
            f"PC=${state['pc']:04X} A=${state['a']:02X} X=${state['x']:02X} "
            f"Y=${state['y']:02X} SP=${state['sp']:02X} P=${state['p']:02X} "
            f"CYCLES={cycles}"
        )

    elif cmd == "SYS":
        if len(parts) < 2:
            return "ERROR: Missing address"
        try:
            addr = int(parts[1].replace('$', '').replace('0x', ''), 16)
            if addr < 0 or addr > 0xFFFF:
                return "ERROR: Address out of range ($0000-$FFFF)"
            emu.cpu.state.pc = addr & 0xFFFF
            return f"OK PC=${addr:04X}"
        except ValueError:
            return f"ERROR: Invalid address format: {parts[1]}"

    elif cmd == "MEMORY":
        if len(parts) < 2:
            return "ERROR: Missing address"
        try:
            addr = int(parts[1].replace('$', '').replace('0x', ''), 16)
        except ValueError:
            return f"ERROR: Invalid address format: {parts[1]}"
        value = emu.memory.read(addr)
        return f"${addr:04X}={value:02X}"

    elif cmd == "WRITE":
        if len(parts) < 3:
            return "ERROR: Missing address or value"
        try:
            addr = int(parts[1].replace('$', '').replace('0x', ''), 16)
            value = int(parts[2].replace('$', '').replace('0x', ''), 16)
        except ValueError:
            return "ERROR: Invalid hex"
        emu.memory.write(addr, value)
        return "OK"

    elif cmd == "DUMP":
        try:
            start = (
                int(parts[1].replace('$', '').replace('0x', ''), 16)
                if len(parts) > 1 else 0x0000
            )
            end = (
                int(parts[2].replace('$', '').replace('0x', ''), 16)
                if len(parts) > 2 else 0x10000
            )
        except ValueError:
            return "ERROR: Invalid hex"
        dump = emu.dump_memory(start, end)
        return dump.hex()

    elif cmd == "SCREEN":
        emu._update_text_screen()
        return emu.render_text_screen(no_colors=True)

    elif cmd == "SEND_KEY":
        if len(parts) < 2:
            return "ERROR: Missing key code"
        try:
            code = _parse_keycode(parts[1]) & 0xFF
        except ValueError:
            return f"ERROR: Invalid key code: {parts[1]}"
        emu.send_petscii(code)
        return "OK"

    elif cmd == "SEND_KEYS":
        if len(parts) < 2:
            return "ERROR: Missing key codes"
        codes = []
        try:
            for raw in parts[1:]:
                codes.append(_parse_keycode(raw) & 0xFF)
        except ValueError as e:
            return f"ERROR: Invalid key code: {e}"
        emu.send_petscii_sequence(codes)
        return "OK"

    elif cmd == "SHOW_KEYBOARD_BUFFER":
        kb_buf_base = KEYBOARD_BUFFER_BASE
        kb_buf_len = emu.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
        codes = [emu.memory.read(kb_buf_base + i) for i in range(kb_buf_len)]
        hex_codes = ' '.join(f"${code:02X}" for code in codes)
        ascii_codes = ''.join(
            chr(code) if 0x20 <= code <= 0x7E else '.' for code in codes
        )
        return f"LEN={kb_buf_len} CODES=[{hex_codes}] ASCII='{ascii_codes}'"

    elif cmd == "SHOW_CURRENT_LINE":
        row, col, line_codes = emu.get_current_line()
        hex_codes = ' '.join(f"${code:02X}" for code in line_codes)
        ascii_line = ''.join(
            chr(code) if 0x20 <= code <= 0x7E else '.' for code in line_codes
        )
        return f"ROW={row} COL={col} LINE='{ascii_line}' CODES=[{hex_codes}]"

    elif cmd == "LOAD":
        if len(parts) < 2:
            return "ERROR: Missing PRG file path"
        try:
            emu.load_prg(parts[1])
            return "OK"
        except Exception as e:
            return f"ERROR: {e}"

    elif cmd == "ATTACH-DISK":
        if len(parts) < 2:
            return "ERROR: Missing D64 file path"
        try:
            disk_path = parts[1]
            device = int(parts[2]) if len(parts) > 2 else 8
            if device < 8 or device > 11:
                return f"ERROR: Invalid device number {device} (must be 8-11)"
            emu.attach_disk(disk_path, device)
            return f"OK: Disk attached to drive {device}"
        except (ValueError, IndexError) as e:
            return f"ERROR: Invalid device number - {e}"
        except Exception as e:
            return f"ERROR: {e}"

    elif cmd == "DETACH-DISKS":
        try:
            emu.detach_disks()
            return "OK: All disks detached"
        except Exception as e:
            return f"ERROR: {e}"

    elif cmd == "STOP":
        emu.running = False
        return "OK"

    elif cmd == "QUIT" or cmd == "EXIT":
        emu.running = False
        return "OK"

    else:
        return f"ERROR: Unknown command '{cmd}'"
