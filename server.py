"""
TCP/UDP Server for controlling the emulator
"""

from __future__ import annotations

import socket
import threading
from typing import Optional, Tuple

from .command_dispatch import dispatch_text_command
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
        """Handle a command and return response.

        Thin wrapper around :func:`dispatch_text_command` that also tears down
        the TCP/UDP server loop on QUIT/EXIT (in addition to the emulator
        shutdown the dispatcher already triggers).
        """
        response = dispatch_text_command(self.emu, command)
        cmd = command.split()[0].upper() if command.split() else ""
        if cmd in ("QUIT", "EXIT"):
            self.running = False
        return response


