#!/usr/bin/env python3
"""Debug script: trace the ATN assertion and drive response phase of the IEC handshake.

This script boots the C64 without the KERNAL shortcut and traces what happens
when the KERNAL asserts ATN (bit 3 of CIA2 PRA = 1) and waits for the drive to
respond by pulling DATA low.

Usage:
    python scripts/debug_iec_atn.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from c64py.emulator import C64
from c64py.d64 import create_blank_d64

ROM_DIR = str(Path(__file__).parent.parent / "roms")
MAX_BOOT_INSN = 4_000_000
MAX_TRACE_INSN = 5_000_000
PRINT_EVERY = 50_000


def make_emu():
    disk = create_blank_d64(disk_name="HELLO", disk_id="01")
    payload = bytes([0x00, 0xC0, 0x11, 0x22, 0x33])
    disk.write_file("HELLO", payload, filetype=0x82)
    d64_path = "/tmp/debug_iec.d64"
    disk.save_to_file(d64_path)

    emu = C64(interface_factory=lambda e: None)
    emu.interface = type("Iface", (), {"add_debug_log": lambda *a, **k: None})()
    emu._initialize_c64()
    emu.load_roms(ROM_DIR, require_char_rom=False)
    emu._iec_disk_full_impl = True
    emu.kernal_load_shortcut_enabled = False   # <--- bit-level path
    emu.initialize_iec_bus(ROM_DIR)
    emu.attach_disk(d64_path, device=8)
    return emu


def main():
    emu = make_emu()

    # Cold reset
    reset_lo = emu.memory.read(0xFFFC)
    reset_hi = emu.memory.read(0xFFFD)
    emu.cpu.state.pc = reset_lo | (reset_hi << 8)
    emu.cpu.state.sp = 0xFD
    emu.cpu.state.p |= 0x04

    drive = emu.iec_drives[8]
    bus = emu.iec_bus

    print("=== Phase 1: Boot to BASIC ===")
    BASIC_START, BASIC_END = 0xA470, 0xA490
    current_cycles = 0
    for i in range(MAX_BOOT_INSN):
        delta = emu.run_cpu_instruction_quantum(current_cycles)
        current_cycles += delta
        emu._step_iec_drives(max(1, delta))
        pc = emu.cpu.state.pc
        if BASIC_START <= pc <= BASIC_END:
            print(f"  BASIC reached at insn {i}, PC=${pc:04X}, drive PC=${drive.cpu.state.pc:04X}")
            break
    else:
        print(f"  BASIC NOT reached after {MAX_BOOT_INSN} insns, PC=${emu.cpu.state.pc:04X}")
        return

    print("=== Phase 2: Warm drive boot (wait for CA1 IRQ enabled) ===")
    for i in range(2_000_000):
        delta = emu.run_cpu_instruction_quantum(current_cycles)
        current_cycles += delta
        emu._step_iec_drives(max(1, delta))
        via1 = drive.memory.via1
        if drive.cpu.state.pc >= 0xEC00 and (via1.ier & 0x02):
            print(f"  Drive ready at insn {i}: PC=${drive.cpu.state.pc:04X} PCR=${via1.pcr:02X} IER=${via1.ier:02X}")
            break
    else:
        via1 = drive.memory.via1
        print(f"  Drive may not have finished boot: PC=${drive.cpu.state.pc:04X} PCR=${via1.pcr:02X} IER=${via1.ier:02X}")

    # Print VIA1 state
    via1 = drive.memory.via1
    print(f"  VIA1: ORB=${via1.orb:02X} DDRB=${via1.ddrb:02X} PCR=${via1.pcr:02X} IER=${via1.ier:02X} IFR=${via1.ifr:02X}")
    print(f"  Bus: ATN={bus.atn} CLK={bus.clk} DATA={bus.data}")
    print(f"  CIA2 PRA=${emu.memory.cia2_pra:02X}")

    print("=== Phase 3: Inject LOAD and trace handshake ===")
    cmd = b'LOAD"HELLO",8,1\r'
    for i, b in enumerate(cmd):
        emu.memory.write(0x0277 + i, b)
    emu.memory.write(0x00C6, len(cmd))

    last_atn = bus.atn
    last_clk = bus.clk
    last_data = bus.data
    last_c64_pc_region = None
    atn_asserted_at = None
    atn_hold_count = 0
    verbose_start = None
    atn_release_at = None
    dense_start = None
    dense_end = None

    def bus_state():
        return f"ATN={'L' if not bus.atn else 'H'} CLK={'L' if not bus.clk else 'H'} DATA={'L' if not bus.data else 'H'}"

    for i in range(MAX_TRACE_INSN):
        delta = emu.run_cpu_instruction_quantum(current_cycles)
        current_cycles += delta
        emu._step_iec_drives(max(1, delta))

        c64pc = emu.cpu.state.pc
        drv_pc = drive.cpu.state.pc
        via1 = drive.memory.via1

        # Detect ATN assertion
        if bus.atn != last_atn:
            print(f"  [{i:8d}] ATN edge {'H→L (ASSERT)' if not bus.atn else 'L→H (RELEASE)'}: "
                  f"C64 PC=${c64pc:04X} DRV PC=${drv_pc:04X} {bus_state()}")
            print(f"           CIA2 PRA=${emu.memory.cia2_pra:02X} VIA1 PCR=${via1.pcr:02X} IER=${via1.ier:02X} IFR=${via1.ifr:02X} CA1={via1._ca1_level}")
            if not bus.atn:
                atn_asserted_at = i
                atn_hold_count = 0
            else:
                atn_release_at = i
                dense_start = i
                dense_end = i + 600   # print 600 insns after release
            last_atn = bus.atn

        # Detect CLK/DATA edges
        if bus.clk != last_clk:
            print(f"  [{i:8d}] CLK edge {'H→L' if not bus.clk else 'L→H'}: "
                  f"C64 PC=${c64pc:04X} DRV PC=${drv_pc:04X} {bus_state()}")
            last_clk = bus.clk

        if bus.data != last_data:
            print(f"  [{i:8d}] DATA edge {'H→L' if not bus.data else 'L→H'}: "
                  f"C64 PC=${c64pc:04X} DRV PC=${drv_pc:04X} {bus_state()}")
            last_data = bus.data

        # Track time ATN is held without DATA response
        if atn_asserted_at is not None and not bus.atn:
            atn_hold_count += 1
            if atn_hold_count == 1000:
                print(f"  [{i:8d}] *** ATN held 1000 insns, no DATA response yet ***")
                print(f"           C64 PC=${c64pc:04X} DRV PC=${drv_pc:04X} {bus_state()}")
                print(f"           VIA1: ORB=${via1.orb:02X} DDRB=${via1.ddrb:02X} PCR=${via1.pcr:02X} IER=${via1.ier:02X} IFR=${via1.ifr:02X}")
                print(f"           VIA1 pb_in=${via1.pb_in:02X} CA1={via1._ca1_level}")
                print(f"           bus.atn_pullers? (bus.atn={bus.atn}) clk_pullers={bus.clk_pullers} data_pullers={bus.data_pullers}")
                verbose_start = i

            if atn_hold_count == 5000:
                print(f"  [{i:8d}] *** ATN held 5000 insns, diagnosing stall ***")
                print(f"           C64 PC=${c64pc:04X} DRV PC=${drv_pc:04X} {bus_state()}")
                print(f"           VIA1: ORB=${via1.orb:02X} DDRB=${via1.ddrb:02X} PCR=${via1.pcr:02X} IER=${via1.ier:02X} IFR=${via1.ifr:02X}")
                print(f"           VIA1 pb_in=${via1.pb_in:02X} irb=${via1.irb:02X} CA1={via1._ca1_level}")
                print(f"           drive pending_irq={drive.memory.pending_irq}")
                print(f"           C64 p={emu.cpu.state.p:02X} (I={bool(emu.cpu.state.p & 0x04)})")
                # Print next 20 drive instructions from current PC
                print(f"  Stall diagnosis: drive in busy loop?")
                # Sample 10 drive PCs quickly
                pcs = set()
                for _ in range(200):
                    drive.step(1)
                    pcs.add(drive.cpu.state.pc)
                print(f"  Drive PCs during 200 steps: {sorted(f'${p:04X}' for p in pcs)[:15]}")
                print(f"  After 200 drive steps: PC=${drive.cpu.state.pc:04X} {bus_state()}")
                break

        # Print verbose trace around ATN assertion
        if verbose_start is not None and i < verbose_start + 200:
            if i % 10 == 0:
                print(f"    [{i:8d}] C64=${c64pc:04X} DRV=${drv_pc:04X} IFR=${via1.ifr:02X} {bus_state()}")

        # Dense trace around ATN release
        if dense_start is not None and dense_start <= i < dense_end:
            print(f"  [{i:8d}] C64=${c64pc:04X} DRV=${drv_pc:04X} {bus_state()} ORB=${via1.orb:02X} pb_in=${via1.pb_in:02X} dp={bus.data_pullers} cp={bus.clk_pullers}")

        # Trace T1 timeout path
        if drv_pc in (0xE9F2, 0xE9F5, 0xE9FA, 0xFF20, 0xFF27, 0xFF29):
            print(f"  [{i:8d}] DRV_KEY=${drv_pc:04X} C64=${c64pc:04X} {bus_state()} ORB=${via1.orb:02X} t1c=${via1.t1c:04X} t1_active={via1.t1_active} IFR=${via1.ifr:02X} dp={bus.data_pullers}")

        # Trace C64 error path around ATN release
        if 0xEDA0 <= c64pc <= 0xEDDC:
            print(f"  [{i:8d}] C64_ERR=${c64pc:04X} DRV=${drv_pc:04X} {bus_state()} CIA2=${emu.memory.cia2_pra:02X} dp={bus.data_pullers}")

        # Detect C64 stuck at $ED5A with ATN released
        if bus.atn and c64pc == 0xED5A and atn_release_at is not None:
            if i - atn_release_at > 200 and (dense_end is None or i > dense_end):
                print(f"  [{i:8d}] *** C64 stuck at $ED5A after ATN release ***")
                print(f"           DRV=${drv_pc:04X} {bus_state()} ORB=${via1.orb:02X} DDRB=${via1.ddrb:02X} pb_in=${via1.pb_in:02X}")
                print(f"           data_pullers={bus.data_pullers} clk_pullers={bus.clk_pullers}")
                drv_pcs = set()
                for _ in range(300):
                    drive.step(1)
                    drv_pcs.add(drive.cpu.state.pc)
                print(f"  Drive PCs over 300 steps: {sorted(f'${p:04X}' for p in drv_pcs)[:20]}")
                break

        # Success check
        if (emu.memory.read(0xC000) == 0x11
                and emu.memory.read(0xC001) == 0x22
                and emu.memory.read(0xC002) == 0x33):
            print(f"  [{i:8d}] *** SUCCESS: payload at $C000-$C002! ***")
            break

        if i % PRINT_EVERY == 0 and i > 0:
            print(f"  [{i:8d}] C64=${c64pc:04X} DRV=${drv_pc:04X} {bus_state()} IFR=${via1.ifr:02X}")

    print("\n=== Final state ===")
    via1 = drive.memory.via1
    print(f"C64 PC=${emu.cpu.state.pc:04X}")
    print(f"Drive PC=${drive.cpu.state.pc:04X}")
    print(f"Bus: {bus_state()}")
    print(f"VIA1: ORB=${via1.orb:02X} DDRB=${via1.ddrb:02X} PCR=${via1.pcr:02X} IER=${via1.ier:02X} IFR=${via1.ifr:02X}")
    print(f"CIA2 PRA=${emu.memory.cia2_pra:02X}")
    print(f"$C000=${emu.memory.read(0xC000):02X}")


if __name__ == "__main__":
    main()
