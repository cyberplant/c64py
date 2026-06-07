"""
drives — Commodore disk drive emulation subpackage.

Public surface:
    drives.c1541_emulator  Drive1541, Drive1541Memory
    drives.drive           DiskDrive
    drives.iec_backend     IECDriveBackend ABC
    drives.tcp_drive_client TcpDriveClient
"""
from .drive import DiskDrive
from .iec_backend import IECDriveBackend

__all__ = ["DiskDrive", "IECDriveBackend"]
