"""
6522 Versatile Interface Adapter (VIA) emulation.

Used by the Commodore 1541 disk drive (two VIAs: VIA1 at $1800 for the IEC
serial bus, VIA2 at $1C00 for the disk head / motor / GCR controller). This
implementation is intentionally minimal — just enough behavior for the 1541
DOS ROM to reach its IEC wait loop and respond to ATN, then service named
LOAD requests through the job-queue trap path. T2 pulse counting on PB6,
shift register modes, and CB2 handshakes are stubbed.

Register map (offsets within $1800 / $1C00 page; mirrored every 16 bytes):

    $0  ORB / IRB        Port B output / input
    $1  ORA / IRA        Port A output / input  (CA2 handshake)
    $2  DDRB             Port B data direction
    $3  DDRA             Port A data direction
    $4  T1C-L            T1 counter low  (R: read counter; W: latch low)
    $5  T1C-H            T1 counter high (R: read counter; W: latch high
                         AND transfer T1L -> T1C, clear T1 IFR, start T1)
    $6  T1L-L            T1 latch low  (write only — does not trigger)
    $7  T1L-H            T1 latch high (write only — does not trigger,
                         does clear T1 IFR)
    $8  T2C-L            T2 counter low  (R: counter; W: latch low)
    $9  T2C-H            T2 counter high (W: transfer + start; clears IFR)
    $A  SR               Shift register (stub)
    $B  ACR              Auxiliary control
    $C  PCR              Peripheral control (CA1/CA2/CB1/CB2 edges)
    $D  IFR              Interrupt flags (W: 1-bits clear)
    $E  IER              Interrupt enable (W bit7=1: set bits; bit7=0: clr)
    $F  ORA/IRA          Port A no-handshake variant

ACR bit map:
    7: T1 PB7 output enable
    6: T1 free-run mode (1=free-run, 0=one-shot)
    5: T2 mode (0=one-shot timer, 1=count PB6 pulses — not implemented)
    4-2: SR mode (not implemented)
    1: PB latch enable
    0: PA latch enable

IFR / IER bit map:
    0: CA2     1: CA1     2: SR    3: CB2
    4: CB1     5: T2      6: T1    7: IRQ (any & IER, R-only on IFR)
"""

from __future__ import annotations

from typing import Callable, Optional


# IFR/IER bits
IFR_CA2 = 0x01
IFR_CA1 = 0x02
IFR_SR  = 0x04
IFR_CB2 = 0x08
IFR_CB1 = 0x10
IFR_T2  = 0x20
IFR_T1  = 0x40
IFR_ANY = 0x80


class VIA6522:
    """Minimal 6522 VIA implementation for 1541 emulation."""

    def __init__(self, name: str = "via", on_pb_write: Optional[Callable[[int, int], None]] = None):
        """Create a fresh VIA in reset state.

        Args:
            name: Debug label.
            on_pb_write: Optional callback(orb, ddrb) fired whenever ORB or DDRB
                changes. The 1541 glue uses this to push port-B output bits to
                the IEC bus immediately.
        """
        self.name = name
        self._on_pb_write = on_pb_write

        # Ports
        self.ora = 0x00     # Output register A
        self.orb = 0x00     # Output register B
        self.ira = 0x00     # Input register A (latched if ACR bit 0)
        self.irb = 0x00     # Input register B (latched if ACR bit 1)
        self.ddra = 0x00
        self.ddrb = 0x00
        # External pin state (driven by the host): 0..255 for both ports.
        self.pa_in = 0xFF
        self.pb_in = 0xFF

        # Control register state
        self.acr = 0x00
        self.pcr = 0x00
        self.ier = 0x00     # bit 7 always reads 1 in real chip but we mask
        self.ifr = 0x00

        # Timer 1
        self.t1c = 0xFFFF   # counter
        self.t1l_l = 0x00   # latch low
        self.t1l_h = 0x00   # latch high
        self.t1_active = False  # counting?
        self.t1_one_shot_fired = False  # one-shot already raised IRQ?

        # Timer 2
        self.t2c = 0xFFFF
        self.t2l_l = 0x00
        self.t2_active = False
        self.t2_one_shot_fired = False

        # Edge inputs (CA1/CB1) — track previous level to detect edges.
        self._ca1_level = True   # released = high
        self._cb1_level = True

    # ------------------------------------------------------------------
    # Public IRQ helper
    # ------------------------------------------------------------------
    @property
    def irq_pending(self) -> bool:
        """True if any enabled IFR bit is set (drives /IRQ low on real chip)."""
        return bool(self.ifr & self.ier & 0x7F)

    def _refresh_ifr_top(self) -> None:
        """Mirror bit7 of IFR from (IFR & IER & 0x7F)."""
        if self.ifr & self.ier & 0x7F:
            self.ifr |= IFR_ANY
        else:
            self.ifr &= 0x7F

    # ------------------------------------------------------------------
    # External pin drivers (called by the 1541 glue layer)
    # ------------------------------------------------------------------
    def set_pa_in(self, value: int) -> None:
        """Set the external level on port A pins (0..255)."""
        value &= 0xFF
        # Latch on positive PCR-defined transition for CA1 — but PA latching is
        # via ACR bit 0. We approximate: when latch enabled, IRA stays at the
        # value sampled at the last CA1 edge; otherwise it follows pa_in live.
        self.pa_in = value
        if not (self.acr & 0x01):
            self.ira = value

    def set_pb_in(self, value: int) -> None:
        """Set the external level on port B pins (0..255)."""
        value &= 0xFF
        self.pb_in = value
        if not (self.acr & 0x02):
            self.irb = value

    def set_ca1(self, level: bool) -> None:
        """Drive CA1 input level. PCR bit 0 selects active edge (0=falling)."""
        rising = self.pcr & 0x01
        old = self._ca1_level
        self._ca1_level = bool(level)
        edge = (old and not level and not rising) or ((not old) and level and rising)
        if edge:
            self.ifr |= IFR_CA1
            # Latch IRA on active CA1 edge if PA latch enabled.
            if self.acr & 0x01:
                self.ira = self.pa_in
            self._refresh_ifr_top()

    def set_cb1(self, level: bool) -> None:
        """Drive CB1 input level. PCR bit 4 selects active edge (0=falling)."""
        rising = self.pcr & 0x10
        old = self._cb1_level
        self._cb1_level = bool(level)
        edge = (old and not level and not rising) or ((not old) and level and rising)
        if edge:
            self.ifr |= IFR_CB1
            if self.acr & 0x02:
                self.irb = self.pb_in
            self._refresh_ifr_top()

    def get_pa_out(self) -> int:
        """Effective driven port-A pin levels: ORA where DDR=1, else hi-Z (1)."""
        return (self.ora & self.ddra) | (~self.ddra & 0xFF)

    def get_pb_out(self) -> int:
        """Effective driven port-B pin levels."""
        return (self.orb & self.ddrb) | (~self.ddrb & 0xFF)

    # ------------------------------------------------------------------
    # Cycle tick
    # ------------------------------------------------------------------
    def tick(self, cycles: int) -> None:
        """Advance internal timers by ``cycles`` host cycles."""
        if cycles <= 0:
            return

        if self.t1_active:
            new = self.t1c - cycles
            if new < 0:
                # Underflowed at least once.
                if self.acr & 0x40:
                    # Free-run: reload from latch and continue, raise IRQ.
                    period = ((self.t1l_h << 8) | self.t1l_l) + 2
                    if period <= 0:
                        period = 1
                    while new < 0:
                        new += period
                        self.ifr |= IFR_T1
                else:
                    # One-shot: fire IRQ once, keep counting down (16-bit wrap).
                    if not self.t1_one_shot_fired:
                        self.ifr |= IFR_T1
                        self.t1_one_shot_fired = True
                    new &= 0xFFFF
                self._refresh_ifr_top()
            self.t1c = new & 0xFFFF

        if self.t2_active and not (self.acr & 0x20):
            # T2 in one-shot timer mode (we don't model PB6 pulse counting).
            new = self.t2c - cycles
            if new < 0:
                if not self.t2_one_shot_fired:
                    self.ifr |= IFR_T2
                    self.t2_one_shot_fired = True
                    self._refresh_ifr_top()
                new &= 0xFFFF
            self.t2c = new & 0xFFFF

    # ------------------------------------------------------------------
    # CPU-facing register access
    # ------------------------------------------------------------------
    def read(self, reg: int) -> int:
        reg &= 0x0F
        if reg == 0x00:    # ORB/IRB
            # Bits where DDRB=1 read ORB; bits where DDRB=0 read pin (or latched).
            if self.acr & 0x02:
                pb = self.irb
            else:
                pb = self.pb_in
            value = (self.orb & self.ddrb) | (pb & ~self.ddrb & 0xFF)
            # Reading IRB clears CB1 (and CB2 if PCR configured).
            self.ifr &= ~IFR_CB1
            self._refresh_ifr_top()
            return value & 0xFF
        elif reg == 0x01:  # ORA/IRA (handshake variant)
            if self.acr & 0x01:
                value = self.ira
            else:
                value = self.pa_in
            self.ifr &= ~(IFR_CA1 | IFR_CA2)
            self._refresh_ifr_top()
            return value & 0xFF
        elif reg == 0x0F:  # ORA/IRA no-handshake
            if self.acr & 0x01:
                return self.ira & 0xFF
            return self.pa_in & 0xFF
        elif reg == 0x02:
            return self.ddrb
        elif reg == 0x03:
            return self.ddra
        elif reg == 0x04:  # T1C-L: read counter low, clear T1 IFR
            self.ifr &= ~IFR_T1
            self._refresh_ifr_top()
            return self.t1c & 0xFF
        elif reg == 0x05:  # T1C-H: read counter high
            return (self.t1c >> 8) & 0xFF
        elif reg == 0x06:
            return self.t1l_l
        elif reg == 0x07:
            return self.t1l_h
        elif reg == 0x08:  # T2C-L: read counter low, clear T2 IFR
            self.ifr &= ~IFR_T2
            self._refresh_ifr_top()
            return self.t2c & 0xFF
        elif reg == 0x09:
            return (self.t2c >> 8) & 0xFF
        elif reg == 0x0A:
            return 0  # SR not implemented
        elif reg == 0x0B:
            return self.acr
        elif reg == 0x0C:
            return self.pcr
        elif reg == 0x0D:
            self._refresh_ifr_top()
            return self.ifr
        elif reg == 0x0E:
            return self.ier | 0x80  # bit 7 reads as 1 per datasheet
        return 0xFF

    def write(self, reg: int, value: int) -> None:
        reg &= 0x0F
        value &= 0xFF
        if reg == 0x00:    # ORB
            self.orb = value
            self.ifr &= ~IFR_CB1
            self._refresh_ifr_top()
            if self._on_pb_write is not None:
                self._on_pb_write(self.orb, self.ddrb)
        elif reg == 0x01:  # ORA (handshake)
            self.ora = value
            self.ifr &= ~(IFR_CA1 | IFR_CA2)
            self._refresh_ifr_top()
        elif reg == 0x0F:  # ORA (no handshake)
            self.ora = value
        elif reg == 0x02:
            self.ddrb = value
            if self._on_pb_write is not None:
                self._on_pb_write(self.orb, self.ddrb)
        elif reg == 0x03:
            self.ddra = value
        elif reg == 0x04:  # T1L-L (write only)
            self.t1l_l = value
        elif reg == 0x05:  # T1C-H: latch high, transfer to counter, start, clear IFR
            self.t1l_h = value
            self.t1c = ((self.t1l_h << 8) | self.t1l_l) & 0xFFFF
            self.ifr &= ~IFR_T1
            self.t1_active = True
            self.t1_one_shot_fired = False
            self._refresh_ifr_top()
        elif reg == 0x06:
            self.t1l_l = value
        elif reg == 0x07:
            self.t1l_h = value
            self.ifr &= ~IFR_T1
            self._refresh_ifr_top()
        elif reg == 0x08:
            self.t2l_l = value
        elif reg == 0x09:  # T2C-H: latch->counter, clear IFR, start
            self.t2c = ((value << 8) | self.t2l_l) & 0xFFFF
            self.ifr &= ~IFR_T2
            self.t2_active = True
            self.t2_one_shot_fired = False
            self._refresh_ifr_top()
        elif reg == 0x0A:
            pass  # SR stub
        elif reg == 0x0B:
            self.acr = value
        elif reg == 0x0C:
            self.pcr = value
        elif reg == 0x0D:
            # Writing 1s clears IFR bits. Bit 7 ignored on write.
            self.ifr &= ~(value & 0x7F)
            self._refresh_ifr_top()
        elif reg == 0x0E:
            # Bit 7 = 1: set bits in IER for 1-bits in value.
            # Bit 7 = 0: clear bits in IER for 1-bits in value.
            if value & 0x80:
                self.ier = (self.ier | (value & 0x7F)) & 0x7F
            else:
                self.ier = (self.ier & ~(value & 0x7F)) & 0x7F
            self._refresh_ifr_top()

    def reset(self) -> None:
        """Hard reset (matches power-on)."""
        self.__init__(name=self.name, on_pb_write=self._on_pb_write)
