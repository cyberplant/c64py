"""
UDP Debug Logger for tracing emulator execution
"""

import json
import queue
import socket
import sys
import threading
import time
from typing import Dict, Tuple, Any, Optional, TextIO

# 6502 opcode mnemonics for VICE-compatible trace output
OPCODE_NAMES = {
    0x00: "BRK", 0x01: "ORA", 0x05: "ORA", 0x06: "ASL", 0x08: "PHP", 0x09: "ORA",
    0x0A: "ASL", 0x0D: "ORA", 0x0E: "ASL", 0x10: "BPL", 0x11: "ORA", 0x15: "ORA",
    0x16: "ASL", 0x18: "CLC", 0x19: "ORA", 0x1D: "ORA", 0x1E: "ASL", 0x20: "JSR",
    0x21: "AND", 0x24: "BIT", 0x25: "AND", 0x26: "ROL", 0x28: "PLP", 0x29: "AND",
    0x2A: "ROL", 0x2C: "BIT", 0x2D: "AND", 0x2E: "ROL", 0x30: "BMI", 0x31: "AND",
    0x35: "AND", 0x36: "ROL", 0x38: "SEC", 0x39: "AND", 0x3D: "AND", 0x3E: "ROL",
    0x40: "RTI", 0x41: "EOR", 0x45: "EOR", 0x46: "LSR", 0x48: "PHA", 0x49: "EOR",
    0x4A: "LSR", 0x4C: "JMP", 0x4D: "EOR", 0x4E: "LSR", 0x50: "BVC", 0x51: "EOR",
    0x55: "EOR", 0x56: "LSR", 0x58: "CLI", 0x59: "EOR", 0x5D: "EOR", 0x5E: "LSR",
    0x60: "RTS", 0x61: "ADC", 0x65: "ADC", 0x66: "ROR", 0x68: "PLA", 0x69: "ADC",
    0x6A: "ROR", 0x6C: "JMP", 0x6D: "ADC", 0x6E: "ROR", 0x70: "BVS", 0x71: "ADC",
    0x75: "ADC", 0x76: "ROR", 0x78: "SEI", 0x79: "ADC", 0x7D: "ADC", 0x7E: "ROR",
    0x81: "STA", 0x84: "STY", 0x85: "STA", 0x86: "STX", 0x88: "DEY", 0x8A: "TXA",
    0x8C: "STY", 0x8D: "STA", 0x8E: "STX", 0x90: "BCC", 0x91: "STA", 0x94: "STY",
    0x95: "STA", 0x96: "STX", 0x98: "TYA", 0x99: "STA", 0x9A: "TXS", 0x9D: "STA",
    0xA0: "LDY", 0xA1: "LDA", 0xA2: "LDX", 0xA4: "LDY", 0xA5: "LDA", 0xA6: "LDX",
    0xA8: "TAY", 0xA9: "LDA", 0xAA: "TAX", 0xAC: "LDY", 0xAD: "LDA", 0xAE: "LDX",
    0xB0: "BCS", 0xB1: "LDA", 0xB4: "LDY", 0xB5: "LDA", 0xB6: "LDX", 0xB8: "CLV",
    0xB9: "LDA", 0xBA: "TSX", 0xBC: "LDY", 0xBD: "LDA", 0xBE: "LDX", 0xC0: "CPY",
    0xC1: "CMP", 0xC4: "CPY", 0xC5: "CMP", 0xC6: "DEC", 0xC8: "INY", 0xC9: "CMP",
    0xCA: "DEX", 0xCC: "CPY", 0xCD: "CMP", 0xCE: "DEC", 0xD0: "BNE", 0xD1: "CMP",
    0xD5: "CMP", 0xD6: "DEC", 0xD8: "CLD", 0xD9: "CMP", 0xDD: "CMP", 0xDE: "DEC",
    0xE0: "CPX", 0xE1: "SBC", 0xE4: "CPX", 0xE5: "SBC", 0xE6: "INC", 0xE8: "INX",
    0xE9: "SBC", 0xEA: "NOP", 0xEC: "CPX", 0xED: "SBC", 0xEE: "INC", 0xF0: "BEQ",
    0xF1: "SBC", 0xF5: "SBC", 0xF6: "INC", 0xF8: "SED", 0xF9: "SBC", 0xFD: "SBC",
    0xFE: "INC",
}

# Opcode addressing modes
OPCODE_MODES = {
    0x00: 'imp', 0x01: 'izx', 0x05: 'zp', 0x06: 'zp', 0x08: 'imp', 0x09: 'imm',
    0x0A: 'acc', 0x0D: 'abs', 0x0E: 'abs', 0x10: 'rel', 0x11: 'izy', 0x15: 'zpx',
    0x16: 'zpx', 0x18: 'imp', 0x19: 'aby', 0x1D: 'abx', 0x1E: 'abx', 0x20: 'abs',
    0x21: 'izx', 0x24: 'zp', 0x25: 'zp', 0x26: 'zp', 0x28: 'imp', 0x29: 'imm',
    0x2A: 'acc', 0x2C: 'abs', 0x2D: 'abs', 0x2E: 'abs', 0x30: 'rel', 0x31: 'izy',
    0x35: 'zpx', 0x36: 'zpx', 0x38: 'imp', 0x39: 'aby', 0x3D: 'abx', 0x3E: 'abx',
    0x40: 'imp', 0x41: 'izx', 0x45: 'zp', 0x46: 'zp', 0x48: 'imp', 0x49: 'imm',
    0x4A: 'acc', 0x4C: 'abs', 0x4D: 'abs', 0x4E: 'abs', 0x50: 'rel', 0x51: 'izy',
    0x55: 'zpx', 0x56: 'zpx', 0x58: 'imp', 0x59: 'aby', 0x5D: 'abx', 0x5E: 'abx',
    0x60: 'imp', 0x61: 'izx', 0x65: 'zp', 0x66: 'zp', 0x68: 'imp', 0x69: 'imm',
    0x6A: 'acc', 0x6C: 'ind', 0x6D: 'abs', 0x6E: 'abs', 0x70: 'rel', 0x71: 'izy',
    0x75: 'zpx', 0x76: 'zpx', 0x78: 'imp', 0x79: 'aby', 0x7D: 'abx', 0x7E: 'abx',
    0x81: 'izx', 0x84: 'zp', 0x85: 'zp', 0x86: 'zp', 0x88: 'imp', 0x8A: 'imp',
    0x8C: 'abs', 0x8D: 'abs', 0x8E: 'abs', 0x90: 'rel', 0x91: 'izy', 0x94: 'zpx',
    0x95: 'zpx', 0x96: 'zpy', 0x98: 'imp', 0x99: 'aby', 0x9A: 'imp', 0x9D: 'abx',
    0xA0: 'imm', 0xA1: 'izx', 0xA2: 'imm', 0xA4: 'zp', 0xA5: 'zp', 0xA6: 'zp',
    0xA8: 'imp', 0xA9: 'imm', 0xAA: 'imp', 0xAC: 'abs', 0xAD: 'abs', 0xAE: 'abs',
    0xB0: 'rel', 0xB1: 'izy', 0xB4: 'zpx', 0xB5: 'zpx', 0xB6: 'zpy', 0xB8: 'imp',
    0xB9: 'aby', 0xBA: 'imp', 0xBC: 'abx', 0xBD: 'abx', 0xBE: 'aby', 0xC0: 'imm',
    0xC1: 'izx', 0xC4: 'zp', 0xC5: 'zp', 0xC6: 'zp', 0xC8: 'imp', 0xC9: 'imm',
    0xCA: 'imp', 0xCC: 'abs', 0xCD: 'abs', 0xCE: 'abs', 0xD0: 'rel', 0xD1: 'izy',
    0xD5: 'zpx', 0xD6: 'zpx', 0xD8: 'imp', 0xD9: 'aby', 0xDD: 'abx', 0xDE: 'abx',
    0xE0: 'imm', 0xE1: 'izx', 0xE4: 'zp', 0xE5: 'zp', 0xE6: 'zp', 0xE8: 'imp',
    0xE9: 'imm', 0xEA: 'imp', 0xEC: 'abs', 0xED: 'abs', 0xEE: 'abs', 0xF0: 'rel',
    0xF1: 'izy', 0xF5: 'zpx', 0xF6: 'zpx', 0xF8: 'imp', 0xF9: 'aby', 0xFD: 'abx',
    0xFE: 'abx',
}

# Opcode sizes (bytes)
OPCODE_SIZES = {
    0x00: 1, 0x01: 2, 0x05: 2, 0x06: 2, 0x08: 1, 0x09: 2, 0x0A: 1, 0x0D: 3, 0x0E: 3,
    0x10: 2, 0x11: 2, 0x15: 2, 0x16: 2, 0x18: 1, 0x19: 3, 0x1D: 3, 0x1E: 3, 0x20: 3,
    0x21: 2, 0x24: 2, 0x25: 2, 0x26: 2, 0x28: 1, 0x29: 2, 0x2A: 1, 0x2C: 3, 0x2D: 3,
    0x2E: 3, 0x30: 2, 0x31: 2, 0x35: 2, 0x36: 2, 0x38: 1, 0x39: 3, 0x3D: 3, 0x3E: 3,
    0x40: 1, 0x41: 2, 0x45: 2, 0x46: 2, 0x48: 1, 0x49: 2, 0x4A: 1, 0x4C: 3, 0x4D: 3,
    0x4E: 3, 0x50: 2, 0x51: 2, 0x55: 2, 0x56: 2, 0x58: 1, 0x59: 3, 0x5D: 3, 0x5E: 3,
    0x60: 1, 0x61: 2, 0x65: 2, 0x66: 2, 0x68: 1, 0x69: 2, 0x6A: 1, 0x6C: 3, 0x6D: 3,
    0x6E: 3, 0x70: 2, 0x71: 2, 0x75: 2, 0x76: 2, 0x78: 1, 0x79: 3, 0x7D: 3, 0x7E: 3,
    0x81: 2, 0x84: 2, 0x85: 2, 0x86: 2, 0x88: 1, 0x8A: 1, 0x8C: 3, 0x8D: 3, 0x8E: 3,
    0x90: 2, 0x91: 2, 0x94: 2, 0x95: 2, 0x96: 2, 0x98: 1, 0x99: 3, 0x9A: 1, 0x9D: 3,
    0xA0: 2, 0xA1: 2, 0xA2: 2, 0xA4: 2, 0xA5: 2, 0xA6: 2, 0xA8: 1, 0xA9: 2, 0xAA: 1,
    0xAC: 3, 0xAD: 3, 0xAE: 3, 0xB0: 2, 0xB1: 2, 0xB4: 2, 0xB5: 2, 0xB6: 2, 0xB8: 1,
    0xB9: 3, 0xBA: 1, 0xBC: 3, 0xBD: 3, 0xBE: 3, 0xC0: 2, 0xC1: 2, 0xC4: 2, 0xC5: 2,
    0xC6: 2, 0xC8: 1, 0xC9: 2, 0xCA: 1, 0xCC: 3, 0xCD: 3, 0xCE: 3, 0xD0: 2, 0xD1: 2,
    0xD5: 2, 0xD6: 2, 0xD8: 1, 0xD9: 3, 0xDD: 3, 0xDE: 3, 0xE0: 2, 0xE1: 2, 0xE4: 2,
    0xE5: 2, 0xE6: 2, 0xE8: 1, 0xE9: 2, 0xEA: 1, 0xEC: 3, 0xED: 3, 0xEE: 3, 0xF0: 2,
    0xF1: 2, 0xF5: 2, 0xF6: 2, 0xF8: 1, 0xF9: 3, 0xFD: 3, 0xFE: 3,
}


class ViceTraceLogger:
    """File-based trace logger with VICE-compatible format for comparison debugging"""

    def __init__(self, filename: str = "c64py_trace.log", wall_time: bool = False):
        self.filename = filename
        self.file: Optional[TextIO] = None
        self.enabled = False
        self._line_count = 0
        self._max_lines = 10000000  # 10M lines for debugging
        self._wall_time = wall_time
        self._wall_last: float = 0.0

    def enable(self) -> None:
        """Enable trace logging to file"""
        try:
            self.file = open(self.filename, 'w')
            self.enabled = True
            self.file.write("; c64py trace (VICE-compatible format)\n")
            self.file.write("; Format: .C:addr  bytes  mnemonic  - A:xx X:xx Y:xx SP:xx flags  cycles\n")
            if self._wall_time:
                self._wall_last = time.monotonic()
                self.file.write(
                    "; wall: each following '; w' line is seconds since previous trace line (monotonic clock)\n"
                )
        except Exception as e:
            print(f"Warning: Failed to open trace file: {e}", file=sys.stderr)
            self.enabled = False
    
    def log_instruction(self, pc: int, opcode: int, operand_bytes: list, 
                        a: int, x: int, y: int, sp: int, flags: int, cycles: int) -> None:
        """Log instruction in VICE-compatible format"""
        if not self.enabled:
            return
        if self._line_count >= self._max_lines:
            if self._line_count == self._max_lines:
                print(f"ViceTraceLogger: reached {self._max_lines} line limit, stopping trace", file=sys.stderr)
                self._line_count += 1  # Prevent repeated message
            return
        
        # Build opcode bytes string (11 chars with padding)
        all_bytes = [opcode] + operand_bytes
        bytes_str = ' '.join(f'{b:02X}' for b in all_bytes).ljust(11)
        
        # Get mnemonic and format operand
        mnemonic = OPCODE_NAMES.get(opcode, '???')
        self._current_pc = pc  # Store for branch target calculation
        operand_str = self._format_operand(opcode, operand_bytes)
        instr_str = f"{mnemonic} {operand_str}".ljust(14)
        
        # Build flags string: NV-BDIZC
        flag_chars = [
            'N' if flags & 0x80 else '.',
            'V' if flags & 0x40 else '.',
            '-',
            'B' if flags & 0x10 else '.',
            'D' if flags & 0x08 else '.',
            'I' if flags & 0x04 else '.',
            'Z' if flags & 0x02 else '.',
            'C' if flags & 0x01 else '.',
        ]
        flags_str = ''.join(flag_chars)
        
        # VICE format: .C:0813  99 FB 00    STA $00FB,Y    - A:D8 X:00 Y:00 SP:f6 N.-..I..  2112858
        line = f".C:{pc:04x}  {bytes_str} {instr_str} - A:{a:02X} X:{x:02X} Y:{y:02X} SP:{sp:02x} {flags_str}  {cycles}\n"

        self.file.write(line)
        self._line_count += 1
        if self._wall_time:
            now = time.monotonic()
            dt = now - self._wall_last
            self._wall_last = now
            self.file.write(f"; w {dt:.9f}\n")

        # Flush periodically
        if self._line_count % 10000 == 0:
            self.file.flush()
    
    def _format_operand(self, opcode: int, operand_bytes: list) -> str:
        """Format operand based on addressing mode"""
        mode = OPCODE_MODES.get(opcode, 'imp')
        if mode == 'acc':
            return 'A'
        if not operand_bytes:
            return ''
        
        if mode == 'imm':
            return f'#${operand_bytes[0]:02X}'
        elif mode == 'zp':
            return f'${operand_bytes[0]:02X}'
        elif mode == 'zpx':
            return f'${operand_bytes[0]:02X},X'
        elif mode == 'zpy':
            return f'${operand_bytes[0]:02X},Y'
        elif mode == 'abs':
            addr = operand_bytes[0] | (operand_bytes[1] << 8)
            return f'${addr:04X}'
        elif mode == 'abx':
            addr = operand_bytes[0] | (operand_bytes[1] << 8)
            return f'${addr:04X},X'
        elif mode == 'aby':
            addr = operand_bytes[0] | (operand_bytes[1] << 8)
            return f'${addr:04X},Y'
        elif mode == 'ind':
            addr = operand_bytes[0] | (operand_bytes[1] << 8)
            return f'(${addr:04X})'
        elif mode == 'izx':
            return f'(${operand_bytes[0]:02X},X)'
        elif mode == 'izy':
            return f'(${operand_bytes[0]:02X}),Y'
        elif mode == 'rel':
            # Calculate actual target address for branches
            # PC is at the branch instruction, +2 for instruction size, then add signed offset
            offset = operand_bytes[0]
            if offset >= 0x80:
                offset -= 0x100  # Convert to signed
            target = (self._current_pc + 2 + offset) & 0xFFFF
            return f'${target:04X}'
        return ''
    
    def close(self) -> None:
        """Close trace file"""
        if self.file:
            self.file.write(f"; Total lines: {self._line_count}\n")
            self.file.close()
            self.file = None
        self.enabled = False


class UdpDebugLogger:
    """UDP debug logger for tracing emulator execution (async)"""

    def __init__(self, port: int = 64738, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.sock = None
        self.enabled = False
        self.queue: queue.Queue[Tuple[str, Dict[str, Any]] | None] = queue.Queue(maxsize=1000000)
        self.worker_thread = None
        self.running = False
        self._seq = 0  # Sequence counter (faster than datetime)
        self._dropped_count = 0
        self._batch_size = 10  # Batch this many messages before sending

    def enable(self) -> None:
        """Enable UDP debug logging"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.enabled = True
            self.running = True
            # Start worker thread for async sending
            self.worker_thread = threading.Thread(target=self._worker, daemon=True)
            self.worker_thread.start()
        except Exception as e:
            print(f"Warning: Failed to create UDP socket for debug: {e}", file=sys.stderr)
            self.enabled = False

    def _worker(self) -> None:
        """Worker thread that sends UDP messages asynchronously with batching"""
        batch: list[bytes] = []
        dest = (self.host, self.port)
        
        while self.running:
            try:
                # Get message from queue with timeout
                item = self.queue.get(timeout=0.05)
                if item is None:  # Shutdown signal
                    # Flush current batch
                    if batch:
                        self.sock.sendto(b''.join(batch), dest)
                        batch.clear()
                    # Flush remaining messages
                    while True:
                        try:
                            remaining = self.queue.get_nowait()
                            if remaining is None:
                                break
                            event_type, data = remaining
                            msg = self._serialize(event_type, data)
                            self.sock.sendto(msg, dest)
                            self.queue.task_done()
                        except queue.Empty:
                            break
                    break
                
                # Serialize in worker thread (not main emulation thread)
                event_type, data = item
                msg = self._serialize(event_type, data)
                batch.append(msg)
                self.queue.task_done()
                
                # Send batch when full or try to drain more without blocking
                if len(batch) >= self._batch_size:
                    self.sock.sendto(b''.join(batch), dest)
                    batch.clear()
                else:
                    # Try to grab more messages without blocking
                    for _ in range(self._batch_size - len(batch)):
                        try:
                            item = self.queue.get_nowait()
                            if item is None:
                                if batch:
                                    self.sock.sendto(b''.join(batch), dest)
                                    batch.clear()
                                break
                            event_type, data = item
                            batch.append(self._serialize(event_type, data))
                            self.queue.task_done()
                        except queue.Empty:
                            break
                    # Send whatever we have
                    if batch:
                        self.sock.sendto(b''.join(batch), dest)
                        batch.clear()
                        
            except queue.Empty:
                # Flush any partial batch on timeout
                if batch:
                    self.sock.sendto(b''.join(batch), dest)
                    batch.clear()
                continue
            except Exception:
                pass  # Silently ignore UDP errors
    
    def _serialize(self, event_type: str, data: Dict) -> bytes:
        """Serialize a message to JSON bytes (called in worker thread)"""
        self._seq += 1
        message = {
            'seq': self._seq,
            'type': event_type,
            'data': data
        }
        return json.dumps(message).encode('utf-8') + b"\n"

    def send(self, event_type: str, data: Dict) -> None:
        """Queue debug event for async sending (non-blocking, zero-copy)"""
        if not self.enabled:
            return

        try:
            # Queue tuple (serialization happens in worker thread)
            self.queue.put_nowait((event_type, data))
        except queue.Full:
            # Queue is full, drop oldest message and add new one
            try:
                self.queue.get_nowait()
                self.queue.put_nowait((event_type, data))
                self._dropped_count += 1
                if self._dropped_count % 1000 == 0:
                    print(f"UDP debug: dropped {self._dropped_count} messages (queue full)")
            except queue.Empty:
                pass
        except Exception:
            pass  # Silently ignore errors

    def close(self) -> None:
        """Close UDP socket and stop worker thread, flushing all pending messages"""
        self.running = False
        if self.queue:
            try:
                self.queue.put_nowait(None)  # Signal shutdown
            except queue.Full:
                pass
            # Flush all pending messages before closing
            # Wait for queue to empty (with timeout)
            timeout = 2.0  # Wait up to 2 seconds for messages to flush
            start_time = time.time()
            while not self.queue.empty() and (time.time() - start_time) < timeout:
                time.sleep(0.01)  # Small delay to let worker process messages
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)  # Increased timeout for flushing
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.enabled = False
