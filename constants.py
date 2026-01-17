"""
C64 Memory Map and I/O Constants
"""

# C64 Memory Map Constants
ROM_BASIC_START = 0xA000
ROM_BASIC_END = 0xC000
ROM_KERNAL_START = 0xE000
ROM_KERNAL_END = 0x10000
ROM_CHAR_START = 0xD000
ROM_CHAR_END = 0xE000

RAM_START = 0x0000
RAM_END = 0x10000

# I/O Addresses
VIC_BASE = 0xD000
SID_BASE = 0xD400
CIA1_BASE = 0xDC00
CIA2_BASE = 0xDD00

# IRQ vector
IRQ_VECTOR = 0x0314

# Screen memory (default)
SCREEN_MEM = 0x0400
COLOR_MEM = 0xD800
SCREEN_COLS = 40
SCREEN_ROWS = 25
SCREEN_SIZE = SCREEN_COLS * SCREEN_ROWS

# Renderer border (text-mode UI)
BORDER_WIDTH = 4
BORDER_HEIGHT = 4

# Cursor state (zero-page)
CURSOR_PTR_LOW = 0xD1
CURSOR_PTR_HIGH = 0xD2
CURSOR_ROW_ADDR = 0xD3
CURSOR_COL_ADDR = 0xD8

# Keyboard buffer (KERNAL)
KEYBOARD_BUFFER_BASE = 0x0277
KEYBOARD_BUFFER_LEN_ADDR = 0xC6
KEYBOARD_BUFFER_SIZE = 10

# BASIC input buffer (screen editor line)
INPUT_BUFFER_BASE = 0x0200
INPUT_BUFFER_INDEX_ADDR = 0x029B
INPUT_BUFFER_LEN_ADDR = 0x029C
# 89 bytes total (88 chars + CR) in the C64 line editor buffer.
BASIC_INPUT_BUFFER_SIZE = 89
BASIC_MAX_LINE_LENGTH = BASIC_INPUT_BUFFER_SIZE - 1

# Cursor blink (KERNAL zero-page variables; used/updated by IRQ on real C64)
# We emulate these so the UI can reflect machine-controlled blink state.
BLNSW = 0x00CC  # Cursor blink enable/state (simplified)
BLNCT = 0x00CD  # Cursor blink counter (simplified)

# Cursor blink cadence. We treat CIA Timer A interrupt as ~60Hz.
CURSOR_BLINK_TICKS = 30  # ~0.5s at 60Hz

# KERNAL addresses and bootstrap heuristics
KERNAL_CHRIN_ADDR = 0xFFCF
BASIC_BOOT_CYCLES = 2020000
STUCK_PC_THRESHOLD = 1000

# VIC-II registers
VIC_MEMORY_CONTROL_REG = 0x18
