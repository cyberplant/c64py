"""
6502 CPU State and CIA Timer
"""

from dataclasses import dataclass


@dataclass
class CPUState:
    """6502 CPU state"""
    pc: int = 0x0000  # Program counter
    a: int = 0  # Accumulator
    x: int = 0  # X register
    y: int = 0  # Y register
    sp: int = 0xFF  # Stack pointer
    p: int = 0x04  # Processor status (I=4 flag set by default on reset, like JSC64)
    cycles: int = 0
    stopped: bool = False
    # Canonical 6502 CLI/SEI/PLP 1-instruction IRQ-poll delay. The flag is set
    # by those opcodes AFTER updating ``p``; the *next* post-instruction IRQ
    # poll uses :attr:`pre_i_flag` (the value effective *before* the
    # instruction touched the I bit) and then clears the flag. See
    # ``test/test_cli_irq_delay.py`` for the canonical regression cases.
    cli_sei_delay: bool = False
    pre_i_flag: int = 0


@dataclass
class CIATimer:
    """CIA timer state"""
    latch: int = 0xFFFF  # Timer latch value
    counter: int = 0xFFFF  # Current counter value
    running: bool = False  # Is timer running?
    irq_enabled: bool = False  # Is IRQ enabled for this timer?
    one_shot: bool = False  # One-shot mode (vs continuous)
    input_mode: int = 0  # Input mode (0=processor clock)

    def update(self, cycles: int) -> bool:
        """Update timer, return True if IRQ should be triggered"""
        if not self.running:
            return False

        if self.input_mode == 0:  # Processor clock mode
            original_counter = self.counter
            self.counter -= cycles

            # Check if we crossed zero (underflow occurred)
            if original_counter > 0 and self.counter <= 0:
                # Timer underflow - reload and generate interrupt
                self.counter = self.latch

                if self.irq_enabled:
                    return True
                # If one-shot, stop timer
                if self.one_shot:
                    self.running = False
        return False

    def reset(self) -> None:
        """Reset timer to latch value"""
        self.counter = self.latch
