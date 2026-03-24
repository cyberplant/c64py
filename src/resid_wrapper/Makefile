# resid_wrapper Makefile
#
# Builds resid_c.so (Linux) or resid_c.dylib (macOS) – a thin C wrapper
# around the reSID C++ library that allows Python ctypes to call reSID
# without dealing with C++ name mangling.
#
# Prerequisites:
#   - reSID headers and (optionally) a compiled static/shared library.
#     On Debian/Ubuntu you can install them from the VICE package:
#       sudo apt install vice-common libresid-builder-dev
#     Or compile reSID from the VICE source tree:
#       https://github.com/VICE-Team/svn-mirror/tree/main/vice/src/resid
#
# Usage:
#   # Build with system-installed reSID headers (e.g. /usr/include/resid/):
#   make RESID_SYSTEM=1
#
#   # Build with VICE source tree reSID:
#   make RESID_SRCDIR=/path/to/vice/src/resid
#
#   # Install to c64py package directory so Python can find it:
#   make install  (or: make DESTDIR=/custom/path install)

CXX      ?= g++
CXXFLAGS ?= -O2 -fPIC -std=c++11
LDFLAGS  ?=

# Optional: location of the reSID static library (libresid.a).
# If empty the linker relies on a shared -lresid or an inline build.
RESID_LIB ?=

# Optional: path to a VICE/reSID source directory (containing sid.h).
RESID_SRCDIR ?=

# Default install target: the c64py package root (two levels up from here).
PKGDIR   ?= $(abspath ../..)
DESTDIR  ?= $(PKGDIR)

# ---------------------------------------------------------------------------

ifeq ($(RESID_SYSTEM),1)
    # Headers installed by a system package (e.g. /usr/include/resid/)
    CXXFLAGS += -DRESID_SYSTEM
    LDFLAGS  += -lresid
else ifneq ($(RESID_SRCDIR),)
    # Headers from a VICE source checkout
    CXXFLAGS += -I$(RESID_SRCDIR)
    RESID_LIB ?= $(RESID_SRCDIR)/.libs/libresid.a
else
    $(warning Neither RESID_SYSTEM nor RESID_SRCDIR is set.)
    $(warning Using a local 'sid.h' – make sure the reSID source is present.)
endif

ifneq ($(RESID_LIB),)
    LDFLAGS += $(RESID_LIB)
endif

UNAME := $(shell uname)
ifeq ($(UNAME),Darwin)
    SOEXT    = dylib
    SOFLAGS  = -dynamiclib
else
    SOEXT    = so
    SOFLAGS  = -shared
endif

TARGET = resid_c.$(SOEXT)

.PHONY: all install clean

all: $(TARGET)

$(TARGET): resid_c.cpp resid_c.h
	$(CXX) $(CXXFLAGS) $(SOFLAGS) -o $@ resid_c.cpp $(LDFLAGS)

install: $(TARGET)
	cp $(TARGET) $(DESTDIR)/

clean:
	rm -f $(TARGET) *.o
