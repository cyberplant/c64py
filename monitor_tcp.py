"""
Minimal TCP remote control for stepping and inspection (not VICE protocol).

Line-oriented ASCII commands, one response per line (may be multi-line for M).
"""

from __future__ import annotations

import queue
import socket
import threading
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from .emulator import C64

# Returned from _dispatch to close the client connection after sending a final line.
_MONITOR_DISCONNECT = object()


def _parse_hex(s: str) -> int:
    s = s.strip().replace("$", "").replace("0x", "")
    return int(s, 16)


class C64MonitorTcpServer:
    """Binds a TCP port; each connection gets a simple command loop."""

    def __init__(self, emu: "C64", port: int, host: str = "127.0.0.1") -> None:
        self.emu = emu
        self.port = int(port)
        self.host = host
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._running = False
        if emu._monitor_cmd_queue is None:
            emu._monitor_cmd_queue = queue.Queue()  # type: ignore[attr-defined]
        if emu._monitor_reply_queue is None:
            emu._monitor_reply_queue = queue.Queue()  # type: ignore[attr-defined]
        emu.cpu._monitor_force_single = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _accept_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(2)
        self._sock = sock
        while self._running:
            try:
                conn, _ = sock.accept()
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        try:
            f = conn.makefile("rwb", buffering=0)
            self._send(
                f,
                "c64py monitor — commands: HELP REGS STEP GO HALT STOP M <addr> [n] "
                "BREAK <addr> CLEARBREAK QUIT\r\n",
            )
            while self._running and self.emu.running:
                line = f.readline()
                if not line:
                    break
                text = line.decode("ascii", errors="replace").strip()
                if not text:
                    continue
                parts = text.split()
                cmd = parts[0].upper()
                try:
                    out = self._dispatch(cmd, parts[1:], f)
                except Exception as exc:
                    out = f"ERROR {exc!r}\r\n"
                if out is _MONITOR_DISCONNECT:
                    self._send(f, "OK bye\r\n")
                    try:
                        f.flush()
                    except OSError:
                        pass
                    break
                self._send(f, out)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, f, s: str) -> None:
        f.write(s.encode("ascii", errors="replace"))

    def _dispatch(self, cmd: str, args: list[str], f) -> Union[str, object]:
        emu = self.emu
        if cmd == "HELP" or cmd == "?":
            return (
                "REGS | STEP | GO | HALT | STOP (stop emulator) | M <hex> [count] | "
                "BREAK <hex> | CLEARBREAK | QUIT (close this connection)\r\n"
            )
        if cmd == "REGS":
            st = emu.cpu.state
            cy = getattr(emu, "current_cycles", st.cycles)
            return (
                f"PC=${st.pc:04X} A=${st.a:02X} X=${st.x:02X} Y=${st.y:02X} "
                f"SP=${st.sp:02X} P=${st.p:02X} CYCLES={cy}\r\n"
            )
        if cmd == "STEP":
            emu._monitor_cmd_queue.put(("STEP",))  # type: ignore[attr-defined]
            try:
                reply = emu._monitor_reply_queue.get(timeout=5.0)  # type: ignore[attr-defined]
            except queue.Empty:
                return "ERROR timeout waiting for STEP\r\n"
            return reply
        if cmd == "GO":
            emu.cpu._monitor_force_single = False
            emu._monitor_cmd_queue.put(("GO",))  # type: ignore[attr-defined]
            return "OK running (batch mode restored)\r\n"
        if cmd == "HALT":
            emu.cpu._monitor_force_single = True
            return "OK single-step mode\r\n"
        if cmd == "STOP":
            emu.running = False
            return "OK emulator stop requested\r\n"
        if cmd == "M":
            if not args:
                return "ERROR M <addr> [count]\r\n"
            addr = _parse_hex(args[0])
            n = int(args[1], 0) if len(args) > 1 else 1
            n = max(1, min(n, 256))
            chunks = []
            for i in range(n):
                b = emu.memory.read((addr + i) & 0xFFFF)
                chunks.append(f"{b:02X}")
            return " ".join(chunks) + "\r\n"
        if cmd == "BREAK":
            if not args:
                return "ERROR BREAK <addr>\r\n"
            emu.monitor_breakpoints.add(_parse_hex(args[0]) & 0xFFFF)
            return "OK\r\n"
        if cmd == "CLEARBREAK":
            emu.monitor_breakpoints.clear()
            return "OK\r\n"
        if cmd == "QUIT":
            return _MONITOR_DISCONNECT
        return f"ERROR unknown {cmd}\r\n"
