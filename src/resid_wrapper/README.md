# reSID C Wrapper

This directory contains a thin C/C++ wrapper around the
[reSID](https://github.com/VICE-Team/svn-mirror/tree/main/vice/src/resid)
library.  It exposes reSID's C++ API through plain C functions so that
Python's `ctypes` module can call them without dealing with C++ name
mangling.

## What is reSID?

reSID is a cycle-accurate, reverse-engineered software emulation of the
MOS 6581/8580 SID (Sound Interface Device) chip used in the Commodore 64.
It was created by Dag Lem and is used by VICE, the Versatile Commodore
Emulator, among other C64 software.

## Prerequisites

You need the reSID C++ headers and, optionally, a compiled static or
shared reSID library.  There are two common ways to get them:

### Option A – System package (Debian/Ubuntu)

```bash
# Install VICE's reSID builder library and headers
sudo apt install vice-common libresid-builder-dev
```

Then build with:

```bash
make RESID_SYSTEM=1
```

### Option B – VICE source tree

Clone (or check out) the VICE SVN mirror and build reSID:

```bash
git clone --depth 1 https://github.com/VICE-Team/svn-mirror.git vice-mirror
cd vice-mirror/vice
./autogen.sh
./configure --enable-option-checking=no
make -C src/resid
```

Then build the wrapper pointing at the reSID source tree:

```bash
make RESID_SRCDIR=/path/to/vice-mirror/vice/src/resid
```

## Building

```bash
# In this directory:
make [RESID_SYSTEM=1 | RESID_SRCDIR=/path/to/resid]
```

This produces `resid_c.so` (Linux) or `resid_c.dylib` (macOS).

## Installing

Copy the shared library to the `c64py` package root so that Python can
find it at runtime:

```bash
make install
# or
make DESTDIR=/path/to/c64py install
```

The `resid.py` module looks for the library in:
1. The `c64py` package directory (alongside `resid.py`).
2. Directories listed in the `RESID_LIB_PATH` environment variable
   (colon-separated on Unix, semicolon-separated on Windows).
3. Standard system library paths (`LD_LIBRARY_PATH`, etc.).

## Usage

After installing the shared library, start c64py with the `--enable-resid`
flag:

```bash
c64py --enable-resid [program.prg]
```

The chip model defaults to MOS 6581.  You can also start with the 8580
model by setting the environment variable:

```bash
RESID_CHIP_MODEL=8580 c64py --enable-resid
```

## API reference (`resid_c.h`)

| Function | Description |
|---|---|
| `resid_create()` | Allocate a new SID instance |
| `resid_destroy(sid)` | Free a SID instance |
| `resid_set_chip_model(sid, model)` | Select MOS6581 (0) or MOS8580 (1) |
| `resid_reset(sid)` | Reset to power-on state |
| `resid_read(sid, offset)` | Read SID register |
| `resid_write(sid, offset, value)` | Write SID register |
| `resid_set_sampling_parameters(sid, clock, method, rate, pass)` | Configure sampling |
| `resid_clock(sid, &delta_t, buf, n)` | Generate PCM samples |
