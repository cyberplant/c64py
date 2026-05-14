"""Host memory command channel.

A guest C64 program (BASIC or ML) drives the emulator by writing into a
fixed pair of 256-byte mailboxes in C64 RAM:

    [TX+0]              size byte (0 = idle, 1..255 = bytes ready)
    [TX+1 .. TX+255]    request payload (guest -> host)

    [RX+0]              size byte (0 = idle, 1..255 = bytes ready)
    [RX+1 .. RX+255]    response payload (host -> guest)

The host polls TX once per CPU quantum. When TX[0] != 0 it copies that
many bytes from TX[1..], clears TX[0] = 0, dispatches the command (text
or JSON depending on the first byte), and writes the reply on RX:
payload first, then RX[0] = N. The guest must clear RX[0] back to 0 to
acknowledge before the host will send the next reply.

Polling happens at the end of each :meth:`emulator.C64.run_cpu_instruction_quantum`
call (including Pygame and Textual CPU loops), not only in :meth:`emulator.C64.run`.

This module is **off by default**; it is enabled only when
``--host-command-ctrl TX=...,RX=...`` is passed on the CLI. It is **not**
a security boundary: the dispatcher reuses the full TCP server grammar
(LOAD/ATTACH-DISK/WRITE/...), which has the same trust as the user
running the emulator.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional, Tuple

from .command_dispatch import dispatch_text_command

if TYPE_CHECKING:
    from .emulator import C64


JSON_SNIFF_BYTE = 0x7B  # ASCII '{'
MAILBOX_SIZE = 256       # 1 size byte + 255 payload bytes
MAX_PAYLOAD = MAILBOX_SIZE - 1


def parse_host_command_ctrl(spec: str) -> Tuple[int, int]:
    """Parse ``--host-command-ctrl TX=0xC000,RX=0xC100`` into (tx, rx).

    Accepts hex (``0x...``, ``$...``) or decimal numbers. Order of TX/RX
    keys is irrelevant. Raises ``ValueError`` on any malformed input,
    out-of-range address, overlap between the two 256-byte regions, or a
    region that would wrap past $FFFF.
    """
    if not spec or not isinstance(spec, str):
        raise ValueError("host-command-ctrl: empty spec")

    parts = [p.strip() for p in spec.split(',') if p.strip()]
    if len(parts) != 2:
        raise ValueError(
            f"host-command-ctrl: expected 'TX=<addr>,RX=<addr>', got {spec!r}"
        )

    found: dict[str, int] = {}
    for part in parts:
        if '=' not in part:
            raise ValueError(
                f"host-command-ctrl: missing '=' in {part!r} "
                f"(expected TX=<addr> or RX=<addr>)"
            )
        key, _, raw = part.partition('=')
        key = key.strip().upper()
        if key not in ('TX', 'RX'):
            raise ValueError(
                f"host-command-ctrl: unknown key {key!r} (expected TX or RX)"
            )
        if key in found:
            raise ValueError(f"host-command-ctrl: duplicate key {key}")
        try:
            found[key] = _parse_addr(raw.strip())
        except ValueError as e:
            raise ValueError(f"host-command-ctrl: bad {key} address: {e}") from None

    if 'TX' not in found or 'RX' not in found:
        raise ValueError("host-command-ctrl: both TX and RX must be specified")

    tx, rx = found['TX'], found['RX']
    for name, addr in (('TX', tx), ('RX', rx)):
        if not (0 <= addr <= 0xFF00):
            raise ValueError(
                f"host-command-ctrl: {name}=${addr:04X} out of range "
                f"(must be $0000-$FF00 so the 256-byte region fits in $0000-$FFFF)"
            )

    # 256-byte regions: [tx, tx+255] and [rx, rx+255]. Overlap test on inclusive ends.
    if not (tx + MAILBOX_SIZE <= rx or rx + MAILBOX_SIZE <= tx):
        raise ValueError(
            f"host-command-ctrl: TX=${tx:04X} and RX=${rx:04X} overlap "
            f"(each region is {MAILBOX_SIZE} bytes)"
        )

    return tx, rx


def _parse_addr(raw: str) -> int:
    s = raw.strip()
    if not s:
        raise ValueError("empty address")
    low = s.lower()
    if s.startswith('$'):
        return int(s[1:], 16)
    if low.startswith('0x'):
        return int(s, 16)
    if low.startswith('0o'):
        return int(s, 8)
    if low.startswith('0b'):
        return int(s, 2)
    # Plain digits -> decimal. Anything else is rejected.
    return int(s, 10)


class HostCommandChannel:
    """Polls TX/RX mailboxes and dispatches commands against a :class:`C64`."""

    def __init__(self, emu: "C64", tx_base: int, rx_base: int) -> None:
        if not (0 <= tx_base <= 0xFF00):
            raise ValueError(f"tx_base out of range: ${tx_base:04X}")
        if not (0 <= rx_base <= 0xFF00):
            raise ValueError(f"rx_base out of range: ${rx_base:04X}")
        if not (tx_base + MAILBOX_SIZE <= rx_base
                or rx_base + MAILBOX_SIZE <= tx_base):
            raise ValueError("TX and RX mailbox regions overlap")
        self.emu = emu
        self.tx_base = tx_base
        self.rx_base = rx_base
        # Diagnostics, useful in tests / debugging
        self.requests_handled = 0
        self.replies_dropped = 0  # guest hasn't acked previous reply
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ poll

    def poll(self) -> bool:
        """Run one poll cycle. Returns True iff a command was dispatched."""
        ram = self.emu.memory.ram  # bytearray, written in-place by Rust batch + Python
        size = ram[self.tx_base]
        if size == 0:
            return False

        # Snapshot payload, then immediately clear TX size so the guest can
        # start preparing the next request even while we're still dispatching.
        n = size & 0xFF
        payload = bytes(ram[self.tx_base + 1: self.tx_base + 1 + n])
        ram[self.tx_base] = 0
        self.requests_handled += 1

        try:
            reply = self._handle(payload)
        except Exception as e:  # never let a handler crash the CPU loop
            self.last_error = repr(e)
            reply = self._encode_error_text(f"unhandled exception: {e!r}",
                                            is_json=self._looks_like_json(payload))

        if reply is None:
            return True

        self._write_reply(reply)
        return True

    # ---------------------------------------------------------------- handle

    @staticmethod
    def _looks_like_json(payload: bytes) -> bool:
        return len(payload) > 0 and payload[0] == JSON_SNIFF_BYTE

    def _handle(self, payload: bytes) -> Optional[bytes]:
        if not payload:
            return b""  # empty request -> empty reply

        if self._looks_like_json(payload):
            return self._handle_json(payload)
        return self._handle_text(payload)

    def _handle_text(self, payload: bytes) -> Optional[bytes]:
        # Decode as latin-1 so every byte round-trips; the dispatcher
        # itself only inspects ASCII letters/digits/punctuation.
        try:
            command = payload.decode('latin-1').strip()
        except Exception as e:
            return self._encode_error_text(f"decode failed: {e!r}", is_json=False)
        reply = dispatch_text_command(self.emu, command)
        return self._encode_text_reply(reply)

    def _handle_json(self, payload: bytes) -> Optional[bytes]:
        try:
            obj = json.loads(payload.decode('utf-8'))
        except UnicodeDecodeError as e:
            return self._encode_error_text(f"utf-8 decode: {e!r}", is_json=True)
        except json.JSONDecodeError as e:
            return self._encode_error_text(f"json parse: {e!r}", is_json=True)

        if not isinstance(obj, dict):
            return self._encode_error_text(
                "json root must be an object", is_json=True
            )

        cmd = obj.get('cmd')
        if not isinstance(cmd, str) or not cmd:
            return self._encode_error_text(
                "missing or empty 'cmd' field", is_json=True
            )
        args = obj.get('args', [])
        if args is None:
            args = []
        if not isinstance(args, list):
            return self._encode_error_text(
                "'args' must be a list", is_json=True
            )

        flat_args: list[str] = []
        for a in args:
            if isinstance(a, str):
                flat_args.append(a)
            elif isinstance(a, (int, float)):
                flat_args.append(str(a))
            else:
                return self._encode_error_text(
                    f"unsupported arg type: {type(a).__name__}", is_json=True
                )

        text_command = ' '.join([cmd] + flat_args)
        reply = dispatch_text_command(self.emu, text_command)
        return self._encode_json_reply({"ok": True, "result": reply})

    # ------------------------------------------------------- reply encoding

    def _encode_text_reply(self, reply: str) -> bytes:
        data = reply.encode('latin-1', errors='replace')
        if len(data) > MAX_PAYLOAD:
            return f"ERROR: reply too long ({len(data)} bytes, max {MAX_PAYLOAD})".encode('ascii')
        return data

    def _encode_json_reply(self, obj: dict) -> bytes:
        data = json.dumps(obj, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        if len(data) > MAX_PAYLOAD:
            err = json.dumps(
                {"ok": False,
                 "error": f"reply too long ({len(data)} bytes, max {MAX_PAYLOAD})"},
                separators=(',', ':'),
            ).encode('ascii')
            # If even the error doesn't fit we have a bigger problem; truncate.
            return err[:MAX_PAYLOAD]
        return data

    def _encode_error_text(self, msg: str, is_json: bool) -> bytes:
        if is_json:
            return self._encode_json_reply({"ok": False, "error": msg})
        return self._encode_text_reply(f"ERROR: {msg}")

    # ----------------------------------------------------------- write reply

    def _write_reply(self, reply: bytes) -> None:
        ram = self.emu.memory.ram
        if ram[self.rx_base] != 0:
            # Guest hasn't consumed the previous reply yet. Drop this one
            # rather than block the CPU loop. Counter helps debugging.
            self.replies_dropped += 1
            return
        n = len(reply)
        if n > MAX_PAYLOAD:
            n = MAX_PAYLOAD
            reply = reply[:MAX_PAYLOAD]
        ram[self.rx_base + 1: self.rx_base + 1 + n] = reply
        # Size byte must be written last so the guest never sees a partial
        # message. This is fine on a single-threaded CPU loop; the guest
        # polls between instructions.
        ram[self.rx_base] = n & 0xFF
