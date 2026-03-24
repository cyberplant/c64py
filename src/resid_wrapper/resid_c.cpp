/*
 * resid_c.cpp - C wrapper implementation for the reSID C++ library
 *
 * This file wraps the reSID C++ classes in plain-C functions so Python's
 * ctypes can call them without dealing with C++ name mangling or vtables.
 *
 * Compilation:
 *   See the provided Makefile or README.md.
 *
 * reSID is copyright (C) Dag Lem <resid@nimrod.no>
 * The VICE-Team fork is at:
 *   https://github.com/VICE-Team/svn-mirror/tree/main/vice/src/resid
 */

#include "resid_c.h"
#include <new>

/*
 * Include the reSID public header.  The exact path depends on how reSID was
 * installed:
 *
 *  - System package (e.g. apt install libresid-builder-dev): <resid/sid.h>
 *  - VICE source tree:  <sid.h> (add -I/path/to/vice/src/resid to CXXFLAGS)
 *  - Standalone build:  adjust the path below or pass -DRESID_HEADER=<...>
 */
#ifndef RESID_HEADER
#  ifdef RESID_SYSTEM
#    define RESID_HEADER <resid/sid.h>
#  else
#    define RESID_HEADER "sid.h"
#  endif
#endif

#include RESID_HEADER

using namespace reSID;

/* Internal struct that hides the C++ SID object from C callers. */
struct resid_sid {
    SID sid;
};

extern "C" {

resid_sid_t *resid_create(void)
{
    resid_sid_t *s = new (std::nothrow) resid_sid_t;
    if (!s) return nullptr;
    /* Default: PAL clock, MOS6581, interpolating resampler, 44100 Hz */
    s->sid.set_chip_model(MOS6581);
    s->sid.set_sampling_parameters(985248.0,
                                   SAMPLE_INTERPOLATE,
                                   44100.0,
                                   -1.0);
    return s;
}

void resid_destroy(resid_sid_t *sid)
{
    delete sid;
}

void resid_set_chip_model(resid_sid_t *sid, int model)
{
    if (!sid) return;
    sid->sid.set_chip_model(model == RESID_MOS8580 ? MOS8580 : MOS6581);
}

void resid_reset(resid_sid_t *sid)
{
    if (!sid) return;
    sid->sid.reset();
}

uint8_t resid_read(resid_sid_t *sid, uint8_t offset)
{
    if (!sid) return 0;
    return sid->sid.read(offset);
}

void resid_write(resid_sid_t *sid, uint8_t offset, uint8_t value)
{
    if (!sid) return;
    sid->sid.write(offset, value);
}

int resid_set_sampling_parameters(resid_sid_t *sid,
                                  double clock_freq,
                                  int method,
                                  double sample_freq,
                                  double pass_freq)
{
    if (!sid) return 0;

    sampling_method sm;
    switch (method) {
        case RESID_SAMPLE_FAST:             sm = SAMPLE_FAST;             break;
        case RESID_SAMPLE_RESAMPLE:         sm = SAMPLE_RESAMPLE;         break;
        case RESID_SAMPLE_RESAMPLE_FASTMEM: sm = SAMPLE_RESAMPLE_FASTMEM; break;
        default:                            sm = SAMPLE_INTERPOLATE;       break;
    }

    return sid->sid.set_sampling_parameters(clock_freq, sm, sample_freq,
                                            pass_freq) ? 1 : 0;
}

int resid_clock(resid_sid_t *sid,
                int *delta_t,
                int16_t *buf,
                int buf_samples)
{
    if (!sid || !delta_t || !buf || buf_samples <= 0) return 0;

    cycle_count dt = static_cast<cycle_count>(*delta_t);
    int n = sid->sid.clock(dt, buf, buf_samples);
    *delta_t = static_cast<int>(dt);
    return n;
}

} /* extern "C" */
