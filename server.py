"""
TCP/UDP Server for controlling the emulator
"""

from __future__ import annotations

import socket
import threading
from typing import Optional, Tuple

from .constants import KEYBOARD_BUFFER_BASE, KEYBOARD_BUFFER_LEN_ADDR
from .emulator import C64

class EmulatorServer:
    """TCP/UDP server for controlling the emulator"""

    def __init__(self, emu: C64, tcp_port: Optional[int] = None, udp_port: Optional[int] = None):
        self.emu = emu
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.running = False

    def start(self) -> None:
        """Start the server"""
        self.running = True

        if self.tcp_port:
            tcp_thread = threading.Thread(target=self._tcp_server, daemon=True)
            tcp_thread.start()
            print(f"TCP server listening on port {self.tcp_port}")

        if self.udp_port:
            udp_thread = threading.Thread(target=self._udp_server, daemon=True)
            udp_thread.start()
            print(f"UDP server listening on port {self.udp_port}")

    def _tcp_server(self) -> None:
        """TCP server thread"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('localhost', self.tcp_port))
        sock.listen(5)

        while self.running:
            try:
                conn, addr = sock.accept()
                threading.Thread(target=self._handle_tcp_client, args=(conn, addr), daemon=True).start()
            except Exception as e:
                if self.running:
                    print(f"TCP server error: {e}")

    def _udp_server(self) -> None:
        """UDP server thread"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('localhost', self.udp_port))

        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                response = self._handle_command(data.decode('utf-8', errors='ignore'))
                if response:
                    sock.sendto(response.encode('utf-8'), addr)
            except Exception as e:
                if self.running:
                    print(f"UDP server error: {e}")

    def _handle_tcp_client(self, conn: socket.socket, addr: Tuple) -> None:
        """Handle TCP client connection"""
        try:
            while self.running:
                data = conn.recv(1024)
                if not data:
                    break
                command = data.decode('utf-8', errors='ignore').strip()
                response = self._handle_command(command)
                if response:
                    conn.sendall(response.encode('utf-8') + b'\n')
        except Exception as e:
            print(f"TCP client error: {e}")
        finally:
            conn.close()

    def _handle_command(self, command: str) -> str:
        """Handle a command and return response"""
        parts = command.split()
        if not parts:
            return "OK"

        cmd = parts[0].upper()
        def parse_keycode(raw: str) -> int:
            cleaned = raw.strip()
            if cleaned.startswith('$') or cleaned.lower().startswith('0x'):
                return int(cleaned.replace('$', '').replace('0x', ''), 16)
            if any(c in 'ABCDEFabcdef' for c in cleaned):
                return int(cleaned, 16)
            return int(cleaned, 10)

        if cmd == "HELP" or cmd == "?":
            return """C64 Emulator TCP Server Commands:
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
STOP                - Stop emulator execution
QUIT/EXIT           - Quit server and emulator
HELP/?              - Show this help message"""

        elif cmd == "STATUS":
            state = self.emu.get_cpu_state()
            # Use current_cycles if available (from emulator.run()), otherwise use cpu.state.cycles
            cycles = getattr(self.emu, 'current_cycles', None)
            if cycles is None:
                cycles = state['cycles']
            return f"PC=${state['pc']:04X} A=${state['a']:02X} X=${state['x']:02X} Y=${state['y']:02X} SP=${state['sp']:02X} P=${state['p']:02X} CYCLES={cycles}"

        elif cmd == "SYS":
            if len(parts) < 2:
                return "ERROR: Missing address"
            try:
                addr = int(parts[1].replace('$', '').replace('0x', ''), 16)
                if addr < 0 or addr > 0xFFFF:
                    return "ERROR: Address out of range ($0000-$FFFF)"
                self.emu.cpu.state.pc = addr & 0xFFFF
                return f"OK PC=${addr:04X}"
            except ValueError as e:
                return f"ERROR: Invalid address format: {parts[1]}"

        elif cmd == "MEMORY":
            if len(parts) < 2:
                return "ERROR: Missing address"
            addr = int(parts[1].replace('$', '').replace('0x', ''), 16)
            value = self.emu.memory.read(addr)
            return f"${addr:04X}={value:02X}"

        elif cmd == "WRITE":
            if len(parts) < 3:
                return "ERROR: Missing address or value"
            addr = int(parts[1].replace('$', '').replace('0x', ''), 16)
            value = int(parts[2].replace('$', '').replace('0x', ''), 16)
            self.emu.memory.write(addr, value)
            return "OK"

        elif cmd == "DUMP":
            start = int(parts[1].replace('$', '').replace('0x', ''), 16) if len(parts) > 1 else 0x0000
            end = int(parts[2].replace('$', '').replace('0x', ''), 16) if len(parts) > 2 else 0x10000
            dump = self.emu.dump_memory(start, end)
            # Return as hex string
            return dump.hex()

        elif cmd == "SCREEN":
            self.emu._update_text_screen()
            # For server mode, always return plain text
            return self.emu.render_text_screen(no_colors=True)

        elif cmd == "SEND_KEY":
            if len(parts) < 2:
                return "ERROR: Missing key code"
            try:
                code = parse_keycode(parts[1]) & 0xFF
            except ValueError:
                return f"ERROR: Invalid key code: {parts[1]}"
            self.emu.send_petscii(code)
            return "OK"

        elif cmd == "SEND_KEYS":
            if len(parts) < 2:
                return "ERROR: Missing key codes"
            codes = []
            try:
                for raw in parts[1:]:
                    codes.append(parse_keycode(raw) & 0xFF)
            except ValueError as e:
                return f"ERROR: Invalid key code: {e}"
            self.emu.send_petscii_sequence(codes)
            return "OK"

        elif cmd == "SHOW_KEYBOARD_BUFFER":
            kb_buf_base = KEYBOARD_BUFFER_BASE
            kb_buf_len = self.emu.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
            codes = [self.emu.memory.read(kb_buf_base + i) for i in range(kb_buf_len)]
            hex_codes = ' '.join(f"${code:02X}" for code in codes)
            ascii_codes = ''.join(chr(code) if 0x20 <= code <= 0x7E else '.' for code in codes)
            return f"LEN={kb_buf_len} CODES=[{hex_codes}] ASCII='{ascii_codes}'"

        elif cmd == "SHOW_CURRENT_LINE":
            row, col, line_codes = self.emu.get_current_line()
            hex_codes = ' '.join(f"${code:02X}" for code in line_codes)
            ascii_line = ''.join(chr(code) if 0x20 <= code <= 0x7E else '.' for code in line_codes)
            return f"ROW={row} COL={col} LINE='{ascii_line}' CODES=[{hex_codes}]"

        elif cmd == "LOAD":
            if len(parts) < 2:
                return "ERROR: Missing PRG file path"
            try:
                self.emu.load_prg(parts[1])
                return "OK"
            except Exception as e:
                return f"ERROR: {e}"

        elif cmd == "STOP":
            self.emu.running = False
            return "OK"

        elif cmd == "QUIT" or cmd == "EXIT":
            self.running = False
            self.emu.running = False
            return "OK"

        else:
            return f"ERROR: Unknown command '{cmd}'"


