"""A4 slice: real ``c1541_emulator`` TCP server + KERNAL wire decode → JSON.

Spawns a headless drive subprocess (same pattern as ``test_disk_integration`` /
``test_fast_load_rpc``), attaches :class:`~c64py.drives.tcp_drive_client.TcpDriveClient`
to an :class:`~c64py.iec_bus.IECBus`, and replays a synthetic CIA2 line sequence
that matches LISTEN + OPEN channel 1 + PRG filename + UNLISTEN. With wire decode
enabled, the client must emit the expected ``listen`` / ``open_channel`` /
``send_byte`` / ``unlisten`` frames to the server.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from typing import Any, Callable, List

import pytest

from c64py.d64 import create_blank_d64
from c64py.drives.tcp_drive_client import TcpDriveClient
from c64py.iec_bus import IECBus
from c64py.iec_kernal_bridge import KernalIecTap
from c64py.memory import MemoryMap


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _append_wired_byte(
    seq: list[tuple[bool, bool, bool]],
    atn_released: bool,
    byte_val: int,
) -> None:
    for bit_i in range(8):
        bit = (byte_val >> bit_i) & 1
        data_high = bit == 0
        seq.append((atn_released, True, data_high))
        seq.append((atn_released, False, data_high))
        seq.append((atn_released, True, data_high))


def _synthetic_listen_open_ch1_prg_unlisten(name: bytes) -> list[tuple[bool, bool, bool]]:
    """LISTEN 8 + OPEN channel 1 (0xF1), ATN up, ASCII filename, UNLISTEN."""
    seq: list[tuple[bool, bool, bool]] = [(True, True, True), (False, True, True)]
    for cmd in (0x28, 0xF1):
        _append_wired_byte(seq, False, cmd)
    seq.append((True, True, True))
    for b in name:
        _append_wired_byte(seq, True, b)
    seq.append((False, True, True))
    _append_wired_byte(seq, False, 0x3F)
    seq.append((True, True, True))
    return seq


def _bus_triple_to_cia2_pra(atn_released: bool, clk_released: bool, data_released: bool) -> int:
    v = 0xFF
    if atn_released:
        v &= ~0x08
    else:
        v |= 0x08
    if clk_released:
        v &= ~0x10
    else:
        v |= 0x10
    if data_released:
        v &= ~0x20
    else:
        v |= 0x20
    return v


@pytest.fixture
def tcp_drive_with_hello_prg(tmp_path):
    d64_path = tmp_path / "iec_wire.d64"
    d = create_blank_d64("WIRETST", "01")
    d.write_file("HELLO", b"\x01\x08\x00\x20\xea", filetype=0x82)
    d.save_to_file(str(d64_path))
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "c64py.drives.c1541_emulator",
            "--interface",
            "headless",
            "--emulation",
            "fast",
            "--disk",
            str(d64_path),
            "--device",
            "8",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert _wait_port(port, timeout=4.0), "drive subprocess did not open port"
    client = TcpDriveClient(device_number=8, host="127.0.0.1", port=port)
    assert client.connect()
    try:
        yield client, port, str(d64_path)
    finally:
        client.disconnect()
        proc.terminate()
        proc.wait(timeout=3)


def test_wire_decode_open_prg_emits_json_to_tcp_server(tcp_drive_with_hello_prg):
    client, _port, _path = tcp_drive_with_hello_prg
    recorded: List[Any] = []
    orig_send: Callable[..., None] = client._send

    def _capture_send(msg: dict) -> None:
        recorded.append(dict(msg))
        orig_send(msg)

    client._send = _capture_send  # type: ignore[method-assign]

    bus = IECBus()
    bus.attach_device(client)

    mem = MemoryMap()
    mem.iec_bus = bus
    tap = KernalIecTap(wire_decode_bus=bus)
    tap.attach_line_receiver(bus)
    mem.iec_kernal_tap = tap

    states = _synthetic_listen_open_ch1_prg_unlisten(b"HELLO")
    mem.cia2_pra = _bus_triple_to_cia2_pra(*states[0])
    mem.apply_cia2_port_a_to_iec_bus()
    for triple in states[1:]:
        mem.cia2_pra = _bus_triple_to_cia2_pra(*triple)
        mem.apply_cia2_port_a_to_iec_bus()

    types = [m.get("type") for m in recorded]
    assert "listen" in types
    assert "open_channel" in types
    open_msgs = [m for m in recorded if m.get("type") == "open_channel"]
    assert open_msgs and open_msgs[0].get("channel") == 1

    payload = [m for m in recorded if m.get("type") == "send_byte"]
    assert len(payload) == 5
    assert [m["byte"] for m in payload] == [0x48, 0x45, 0x4C, 0x4C, 0x4F]
    assert payload[-1].get("eoi") is True

    assert types.count("unlisten") >= 1

    client.step(64)
    data, err, _ft = client.fast_load("HELLO", secondary=1)
    assert err is None and data is not None and len(data) >= 2
