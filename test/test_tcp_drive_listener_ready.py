"""TcpDriveClient simulates IEC listener DATA pull so KERNAL can leave $DD00 polls."""

from __future__ import annotations

from c64py.drives.tcp_drive_client import TcpDriveClient
from c64py.iec_bus import IECBus


def test_tcp_pulls_data_after_atn_release_following_data_secondary() -> None:
    bus = IECBus()
    client = TcpDriveClient(8, "127.0.0.1", 59999)
    bus.attach_device(client)
    bus.deliver_command(0x28)
    bus.deliver_command(0x61)
    assert client._await_listen_ready is True
    assert not bus.data_pullers
    bus.set_atn(False)
    bus.set_atn(True)
    assert client._iec_peer_tag in bus.data_pullers
    assert client._listen_data_low is True
    assert client._await_listen_ready is False


def test_tcp_rearms_data_ready_after_each_received_byte() -> None:
    bus = IECBus()
    client = TcpDriveClient(8, "127.0.0.1", 59999)
    bus.attach_device(client)
    bus.deliver_command(0x28)
    bus.deliver_command(0x61)
    bus.set_atn(False)
    bus.set_atn(True)
    assert client._iec_peer_tag in bus.data_pullers
    client.iec_receive_byte(0x41, eoi=False)
    assert client._iec_peer_tag in bus.data_pullers


def test_talk_secondary_does_not_arm_listen_ready() -> None:
    bus = IECBus()
    client = TcpDriveClient(8, "127.0.0.1", 59999)
    bus.attach_device(client)
    bus.deliver_command(0x48)
    bus.deliver_command(0x60)
    assert client._await_listen_ready is False
