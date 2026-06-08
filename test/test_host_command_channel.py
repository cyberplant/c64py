"""Unit tests for the host memory command channel.

These tests don't require ROMs: they construct a stub emulator with just
enough surface (a `memory.ram` bytearray and a few methods) for the
HostCommandChannel + dispatch_text_command to operate. A small wiring
smoke test verifies the run loop in `emulator.run` invokes `poll()`.
"""

from __future__ import annotations

import json
import pytest

from c64py.host_command_channel import (
    HostCommandChannel,
    JSON_SNIFF_BYTE,
    MAX_PAYLOAD,
    parse_host_command_ctrl,
)


# --------------------------------------------------------------------- parser

class TestParseHostCommandCtrl:
    def test_hex_0x_form(self):
        assert parse_host_command_ctrl("TX=0xC000,RX=0xC100") == (0xC000, 0xC100)

    def test_hex_dollar_form(self):
        assert parse_host_command_ctrl("TX=$C000,RX=$C100") == (0xC000, 0xC100)

    def test_decimal(self):
        assert parse_host_command_ctrl("TX=49152,RX=49408") == (49152, 49408)

    def test_order_irrelevant(self):
        assert parse_host_command_ctrl("RX=0xC100,TX=0xC000") == (0xC000, 0xC100)

    def test_lowercase_keys(self):
        assert parse_host_command_ctrl("tx=0xC000,rx=0xC100") == (0xC000, 0xC100)

    def test_overlap_rejected_exact(self):
        with pytest.raises(ValueError, match="overlap"):
            parse_host_command_ctrl("TX=0xC000,RX=0xC000")

    def test_overlap_rejected_partial(self):
        # RX starts inside TX's 256-byte region.
        with pytest.raises(ValueError, match="overlap"):
            parse_host_command_ctrl("TX=0xC000,RX=0xC080")

    def test_adjacent_regions_allowed(self):
        # TX = $C000..$C0FF, RX = $C100..$C1FF — touching, not overlapping.
        assert parse_host_command_ctrl("TX=0xC000,RX=0xC100") == (0xC000, 0xC100)

    def test_address_too_high(self):
        with pytest.raises(ValueError, match="out of range"):
            parse_host_command_ctrl("TX=0xFF80,RX=0xC000")

    def test_missing_key(self):
        with pytest.raises(ValueError):
            parse_host_command_ctrl("TX=0xC000")

    def test_unknown_key(self):
        with pytest.raises(ValueError, match="unknown key"):
            parse_host_command_ctrl("TX=0xC000,FOO=0xC100")

    def test_duplicate_key(self):
        with pytest.raises(ValueError, match="duplicate"):
            parse_host_command_ctrl("TX=0xC000,TX=0xC100")

    def test_empty_spec(self):
        with pytest.raises(ValueError):
            parse_host_command_ctrl("")

    def test_garbage(self):
        with pytest.raises(ValueError):
            parse_host_command_ctrl("not a spec")


# ---------------------------------------------------------------- channel ops

class _FakeMemory:
    def __init__(self):
        self.ram = bytearray(0x10000)

    def read(self, addr: int) -> int:
        return self.ram[addr & 0xFFFF]

    def write(self, addr: int, value: int) -> None:
        self.ram[addr & 0xFFFF] = value & 0xFF


class _FakeEmu:
    """Minimal stand-in for C64 supporting a few text commands.

    We register a tiny subset of commands so we can drive the channel
    without bringing up real ROMs. The dispatcher used by the channel
    is the real one (`dispatch_text_command`), so we provide the methods
    it actually calls (`get_cpu_state`, etc.).
    """

    def __init__(self):
        self.memory = _FakeMemory()
        self.running = True
        self.current_cycles = 1234
        # Minimal CPU shim for STATUS and SYS.
        self.cpu = type("CPU", (), {})()
        self.cpu.state = type("State", (), {})()
        self.cpu.state.pc = 0x1000
        self.cpu.state.a = 0x11
        self.cpu.state.x = 0x22
        self.cpu.state.y = 0x33
        self.cpu.state.sp = 0xF7
        self.cpu.state.p = 0x24
        self.cpu.state.cycles = 999

    def get_cpu_state(self):
        s = self.cpu.state
        return {
            "pc": s.pc, "a": s.a, "x": s.x, "y": s.y,
            "sp": s.sp, "p": s.p, "cycles": s.cycles,
        }


def _make_channel(tx=0xC000, rx=0xC100):
    emu = _FakeEmu()
    chan = HostCommandChannel(emu, tx, rx)
    return emu, chan


def _post_request(emu, tx, payload: bytes):
    n = len(payload)
    assert 0 < n <= MAX_PAYLOAD
    emu.memory.ram[tx + 1: tx + 1 + n] = payload
    emu.memory.ram[tx] = n


def _read_reply(emu, rx) -> bytes:
    n = emu.memory.ram[rx]
    if n == 0:
        return b""
    return bytes(emu.memory.ram[rx + 1: rx + 1 + n])


class TestHostCommandChannelText:
    def test_idle_poll_returns_false(self):
        _, chan = _make_channel()
        assert chan.poll() is False

    def test_status_round_trip(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b"STATUS")
        assert chan.poll() is True
        # TX cleared
        assert emu.memory.ram[0xC000] == 0
        reply = _read_reply(emu, 0xC100).decode("ascii")
        assert "PC=$1000" in reply
        assert "A=$11" in reply
        assert "CYCLES=1234" in reply

    def test_unknown_command(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b"FROBNICATE")
        chan.poll()
        reply = _read_reply(emu, 0xC100)
        assert reply.startswith(b"ERROR:")

    def test_quit_clears_emu_running(self):
        emu, chan = _make_channel()
        assert emu.running is True
        _post_request(emu, 0xC000, b"QUIT")
        chan.poll()
        assert emu.running is False

    def test_help_truncated_too_long(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b"HELP")
        chan.poll()
        reply = _read_reply(emu, 0xC100)
        # HELP is multi-line, almost certainly > 255 bytes.
        assert reply.startswith(b"ERROR: reply too long")
        assert len(reply) <= MAX_PAYLOAD

    def test_replies_dropped_when_guest_holds_rx(self):
        emu, chan = _make_channel()
        # Pretend the guest hasn't acked a previous reply.
        emu.memory.ram[0xC100] = 5
        _post_request(emu, 0xC000, b"STATUS")
        chan.poll()
        assert chan.replies_dropped == 1
        # RX size byte untouched.
        assert emu.memory.ram[0xC100] == 5

    def test_back_to_back_requests(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b"STATUS")
        chan.poll()
        # Guest reads + acks reply.
        emu.memory.ram[0xC100] = 0
        _post_request(emu, 0xC000, b"STATUS")
        chan.poll()
        reply = _read_reply(emu, 0xC100).decode("ascii")
        assert "PC=$1000" in reply
        assert chan.requests_handled == 2
        assert chan.replies_dropped == 0


class TestHostCommandChannelJson:
    def test_json_sniff_byte(self):
        # Sanity: the sniff byte is ASCII '{'.
        assert JSON_SNIFF_BYTE == ord("{")

    def test_json_status(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{"cmd":"STATUS"}')
        chan.poll()
        reply = _read_reply(emu, 0xC100).decode("ascii")
        obj = json.loads(reply)
        assert obj["ok"] is True
        assert "PC=$1000" in obj["result"]

    def test_json_with_args(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{"cmd":"WRITE","args":["$C200","$AB"]}')
        chan.poll()
        reply = _read_reply(emu, 0xC100).decode("ascii")
        obj = json.loads(reply)
        assert obj["ok"] is True
        assert emu.memory.ram[0xC200] == 0xAB

    def test_json_missing_cmd(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{"foo":"bar"}')
        chan.poll()
        obj = json.loads(_read_reply(emu, 0xC100).decode("ascii"))
        assert obj["ok"] is False
        assert "cmd" in obj["error"].lower()

    def test_json_malformed(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{not json')
        chan.poll()
        obj = json.loads(_read_reply(emu, 0xC100).decode("ascii"))
        assert obj["ok"] is False
        assert "json" in obj["error"].lower()

    def test_json_root_must_be_object(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{"a":1}')  # OK shape but missing cmd
        chan.poll()
        obj = json.loads(_read_reply(emu, 0xC100).decode("ascii"))
        assert obj["ok"] is False

    def test_json_args_not_list(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{"cmd":"STATUS","args":"oops"}')
        chan.poll()
        obj = json.loads(_read_reply(emu, 0xC100).decode("ascii"))
        assert obj["ok"] is False
        assert "args" in obj["error"].lower()

    def test_json_dispatcher_error(self):
        emu, chan = _make_channel()
        _post_request(emu, 0xC000, b'{"cmd":"NO_SUCH_COMMAND"}')
        chan.poll()
        obj = json.loads(_read_reply(emu, 0xC100).decode("ascii"))
        assert obj["ok"] is False
        assert "unknown command" in obj["error"].lower()
        assert "result" not in obj


class TestHostCommandChannelConstruction:
    def test_overlap_rejected(self):
        emu = _FakeEmu()
        with pytest.raises(ValueError, match="overlap"):
            HostCommandChannel(emu, 0xC000, 0xC080)

    def test_out_of_range_rejected(self):
        emu = _FakeEmu()
        with pytest.raises(ValueError):
            HostCommandChannel(emu, 0xFF80, 0xC000)


# --------------------------------------------------- emulator.run wiring smoke

def test_run_cpu_instruction_quantum_polls_channel(monkeypatch):
    """Host command channel must be polled from run_cpu_instruction_quantum.

    Graphics and Textual UIs drive the CPU via that method instead of C64.run(),
    so poll cannot live only in the run() loop.
    """
    from c64py import emulator as emu_mod

    src = open(emu_mod.__file__).read()
    assert "def run_cpu_instruction_quantum" in src
    assert "def _poll_host_cmd_channel" in src
    assert "self._host_cmd_channel.poll()" in src, (
        "run_cpu_instruction_quantum must end with HostCommandChannel.poll()"
    )
