"""
drives/tcp_drive_client.py — TCP client that makes a remote drive server look
like a local IEC bus device.

The client connects to a ``c1541_emulator`` server (or any compatible server,
e.g. an ESP32 running the same JSON protocol) and translates every IEC hook
call into a JSON frame sent over the socket.

Usage:
    from c64py.drives.tcp_drive_client import TcpDriveClient
    from c64py.iec_bus import IECBus

    bus = IECBus()
    client = TcpDriveClient(device_number=8, host="localhost", port=6408)
    client.connect()          # call once before attaching
    bus.attach_device(client)

The client is intentionally synchronous and non-blocking: ``step()`` drains
whatever reply bytes are already in the OS socket buffer without waiting.
This keeps the emulator main loop from stalling on network I/O.

For ``PRINT#`` / LISTEN data-secondaries, the stock KERNAL waits in a tight
``$DD00`` read loop until the listener pulls DATA low ("ready"). A real 1541
does this via VIA; :class:`TcpDriveClient` asserts DATA on the logical
:class:`~c64py.iec_bus.IECBus` when ATN is released after a data-secondary and
re-asserts after each logical ``iec_receive_byte`` so CIA2 reads can progress
while JSON is proxied to the TCP server.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Optional, Tuple

from .iec_backend import IECDriveBackend

log = logging.getLogger(__name__)


class TcpDriveClient(IECDriveBackend):
    """IEC bus device that proxies commands to a remote drive server over TCP."""

    RECONNECT_DELAY = 5.0

    def __init__(self, device_number: int, host: str = "localhost",
                 port: int = 6400) -> None:
        self.device_number = device_number
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._recv_buf = b""
        self._connected = False
        self._last_disconnect: float = 0.0
        # Simulated open-collector DATA pull for KERNAL bit-bang "listener ready"
        # (see :meth:`on_atn_changed` / :meth:`iec_receive_byte`).
        self._iec_peer_tag = f"tcp_drv_{int(device_number)}"
        self._listen_data_low: bool = False
        self._await_listen_ready: bool = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Open the TCP connection to the drive server.

        Returns True on success, False if the server is not reachable.
        Idempotent — safe to call multiple times.
        """
        if self._connected:
            return True
        # Honour the reconnect back-off so we don't hammer a down server.
        if self._last_disconnect and \
                time.monotonic() - self._last_disconnect < self.RECONNECT_DELAY:
            return False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.host, self.port))
            self._sock = s
            self._connected = True
            log.info("Connected to drive %d at %s:%d", self.device_number,
                     self.host, self.port)
            # Drain the greeting message ("ready") with a short blocking read
            # so it doesn't pollute the first _request() reply.
            s.settimeout(2.0)
            try:
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(256)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            s.setblocking(False)
            return True
        except OSError as exc:
            log.warning("Cannot connect to drive %d at %s:%d: %s",
                        self.device_number, self.host, self.port, exc)
            self._sock = None
            self._connected = False
            self._last_disconnect = time.monotonic()
            return False

    def disconnect(self) -> None:
        """Close the TCP connection."""
        self._release_listen_data_line()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False
        self._last_disconnect = time.monotonic()

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Simulated IEC DATA (listener ready) for KERNAL $DD00 polling loops
    # ------------------------------------------------------------------

    def _release_listen_data_line(self) -> None:
        bus = self.iec_bus
        if bus is None or not self._listen_data_low:
            self._listen_data_low = False
            return
        bus.set_data(self._iec_peer_tag, True)
        self._listen_data_low = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _mark_disconnected(self) -> None:
        """Record a connection loss and close the socket."""
        self._release_listen_data_line()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        was_connected = self._connected
        self._connected = False
        self._recv_buf = b""
        self._last_disconnect = time.monotonic()
        if was_connected:
            log.warning(
                "Drive %d connection lost — will retry in %.0fs",
                self.device_number, self.RECONNECT_DELAY,
            )

    def _ensure_connected(self) -> bool:
        """Try to (re)connect if not currently connected.  Returns True if ready."""
        if self._connected:
            return True
        return self.connect()

    def _send(self, msg: dict) -> None:
        """Serialise ``msg`` as JSON and write it to the socket."""
        if not self._ensure_connected() or self._sock is None:
            return
        try:
            data = json.dumps(msg).encode() + b"\n"
            self._sock.sendall(data)
        except OSError as exc:
            log.warning("Send error (drive %d): %s", self.device_number, exc)
            self._mark_disconnected()

    def _flush_replies(self) -> list[dict]:
        """Read all pending reply frames from the socket (non-blocking)."""
        if not self._connected or self._sock is None:
            return []
        replies = []
        while True:
            try:
                chunk = self._sock.recv(4096)
            except BlockingIOError:
                break
            except OSError as exc:
                log.warning("Recv error (drive %d): %s", self.device_number, exc)
                self._mark_disconnected()
                break
            if not chunk:
                self._mark_disconnected()
                break
            self._recv_buf += chunk

        while b"\n" in self._recv_buf:
            line, self._recv_buf = self._recv_buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                replies.append(json.loads(line.decode()))
            except json.JSONDecodeError:
                log.debug("Bad JSON from drive %d: %r", self.device_number, line)
        return replies

    def _request(self, msg: dict) -> Optional[dict]:
        """Send ``msg`` and block until one reply frame arrives (with timeout)."""
        if not self._ensure_connected() or self._sock is None:
            return None
        self._send(msg)
        if not self._connected or self._sock is None:
            return None
        # Switch to blocking mode with a short timeout to get the reply.
        try:
            self._sock.setblocking(True)
            self._sock.settimeout(2.0)
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    self._mark_disconnected()
                    return None
                self._recv_buf += chunk
                if b"\n" in self._recv_buf:
                    break
        except OSError as exc:
            log.warning("Request error (drive %d): %s", self.device_number, exc)
            self._mark_disconnected()
            return None
        finally:
            try:
                if self._sock:
                    self._sock.setblocking(False)
            except OSError:
                pass

        line, self._recv_buf = self._recv_buf.split(b"\n", 1)
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # IECDriveBackend — bus-level callbacks
    # ------------------------------------------------------------------

    def notify_bus_change(self) -> None:
        pass

    def on_atn_changed(self, atn_state: bool) -> None:
        """Pull DATA low once when ATN is released after a data-secondary (PRINT#)."""
        if not self._await_listen_ready:
            return
        bus = self.iec_bus
        if bus is None or not atn_state:
            return
        if bus.secondary_phase != "data":
            return
        if bus.talker is not None:
            return
        if bus.listener != self.device_number and bus.current_listener != self.device_number:
            return
        bus.set_data(self._iec_peer_tag, False)
        self._listen_data_low = True
        self._await_listen_ready = False

    # ------------------------------------------------------------------
    # IECDriveBackend — byte-level protocol callbacks
    # ------------------------------------------------------------------

    def on_listen(self) -> None:
        self._await_listen_ready = False
        self._release_listen_data_line()
        self._send({"type": "listen", "device": self.device_number})

    def on_unlisten(self) -> None:
        self._await_listen_ready = False
        self._release_listen_data_line()
        self._send({"type": "unlisten"})

    def on_talk(self) -> None:
        self._await_listen_ready = False
        self._release_listen_data_line()
        self._send({"type": "talk", "device": self.device_number})

    def on_untalk(self) -> None:
        self._await_listen_ready = False
        self._release_listen_data_line()
        self._send({"type": "untalk"})

    def on_secondary_address(self, channel: int) -> None:
        pass

    def iec_open_channel(self, channel: int) -> None:
        self._send({"type": "open_channel", "channel": channel})

    def iec_close_channel(self, channel: int) -> None:
        self._send({"type": "close_channel", "channel": channel})

    def iec_secondary(self, channel: int, kind: str) -> None:
        self._send({"type": "secondary", "channel": channel, "kind": kind})
        if kind == "data":
            bus = self.iec_bus
            if (
                bus is not None
                and bus.talker is None
                and (
                    bus.listener == self.device_number
                    or bus.current_listener == self.device_number
                )
            ):
                self._await_listen_ready = True

    def iec_unlisten(self) -> None:
        self._await_listen_ready = False
        self._release_listen_data_line()
        self._send({"type": "unlisten"})

    def iec_untalk(self) -> None:
        self._await_listen_ready = False
        self._release_listen_data_line()
        self._send({"type": "untalk"})

    def iec_receive_byte(self, byte: int, eoi: bool = False) -> None:
        self._release_listen_data_line()
        self._send({"type": "send_byte", "byte": byte & 0xFF, "eoi": bool(eoi)})
        bus = self.iec_bus
        if (
            bus is not None
            and bus.atn
            and bus.secondary_phase == "data"
            and bus.listener == self.device_number
            and bus.talker is None
        ):
            bus.set_data(self._iec_peer_tag, False)
            self._listen_data_low = True

    def iec_send_byte(self) -> Optional[Tuple[int, bool]]:
        reply = self._request({"type": "request_byte"})
        if reply is None:
            return None
        if reply.get("type") == "byte":
            return (int(reply["byte"]) & 0xFF, bool(reply.get("eoi", False)))
        return None

    # ------------------------------------------------------------------
    # Emulator integration
    # ------------------------------------------------------------------

    def step(self, cycles: int = 1) -> int:
        """Drain pending reply frames from the socket buffer (non-blocking).

        The remote server runs its own event loop; we don't need to step it.
        This method just keeps the receive buffer from filling up.
        """
        self._flush_replies()
        return 0

    @property
    def led_on(self) -> bool:
        """Drive activity LED state. Always False for remote drives."""
        return False

    # ------------------------------------------------------------------
    # Fast LOAD/SAVE RPC helpers
    # ------------------------------------------------------------------

    def fast_load(self, filename: str, secondary: int = 0) -> tuple:
        """Synchronous fast-LOAD RPC.

        Returns ``(data_bytes, None, dos_filetype_nibble)`` on success, or
        ``(None, (code, message), None)`` on error. ``dos_filetype_nibble`` is
        ``1`` SEQ, ``2`` PRG, ``3`` USR, ``4`` REL (used by KERNAL hook for
        secondary-address semantics).
        """
        import base64
        reply = self._request({"type": "fast_load", "filename": filename,
                               "secondary": int(secondary)})
        if reply is None or reply.get("type") != "fast_load_reply":
            return None, (74, "DRIVE NOT READY"), None
        if not reply.get("ok"):
            return None, (int(reply.get("error_code", 62)),
                          str(reply.get("error_message", "FILE NOT FOUND"))), None
        dos_ft = reply.get("dos_filetype")
        if dos_ft is not None:
            try:
                dos_ft = int(dos_ft)
            except (TypeError, ValueError):
                dos_ft = 2
        else:
            dos_ft = 2
        return base64.b64decode(reply["data"]), None, dos_ft

    def fast_save(self, filename: str, data: bytes) -> tuple:
        """Synchronous fast-SAVE RPC.

        Returns (True, None) on success or (False, (code, message)) on error.
        """
        import base64
        reply = self._request({"type": "fast_save", "filename": filename,
                               "data": base64.b64encode(bytes(data)).decode("ascii")})
        if reply is None or reply.get("type") != "fast_save_reply":
            return False, (74, "DRIVE NOT READY")
        if not reply.get("ok"):
            return False, (int(reply.get("error_code", 63)),
                           str(reply.get("error_message", "FILE EXISTS")))
        return True, None

    def attach_disk_remote(self, path: str) -> bool:
        """Ask the drive server to attach a D64 image at *path*."""
        reply = self._request({"type": "attach_disk", "path": path})
        return bool(reply and reply.get("ok"))

    def detach_disk_remote(self) -> bool:
        """Ask the drive server to detach the current disk image."""
        reply = self._request({"type": "detach_disk"})
        return bool(reply and reply.get("ok"))

    def get_remote_status(self) -> Optional[dict]:
        """Return the server status dict (led_on, disk, status) or None."""
        return self._request({"type": "status"})

    # Legacy hooks for IECBus back-compat
    def receive_byte(self, byte: int) -> None:
        self.iec_receive_byte(byte, eoi=False)

    def send_byte(self) -> Optional[int]:
        result = self.iec_send_byte()
        if result is None:
            return None
        return result[0]
