/*
 * resid_c.h - C API wrapper for the reSID C++ library
 *
 * This header exposes reSID functionality through a plain C interface so it
 * can be called from Python via ctypes without dealing with C++ name mangling.
 *
 * Build the shared library (resid_c.so / resid_c.dylib) using the provided
 * Makefile, then load it from Python with ctypes.
 *
 * reSID is copyright (C) Dag Lem <resid@nimrod.no>
 * The VICE-Team fork is at:
 *   https://github.com/VICE-Team/svn-mirror/tree/main/vice/src/resid
 */

#ifndef RESID_C_H
#define RESID_C_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/* Opaque handle to a reSID SID instance. */
typedef struct resid_sid resid_sid_t;

/* Chip model constants (mirrors reSID's chip_model enum). */
#define RESID_MOS6581 0
#define RESID_MOS8580 1

/* Sampling method constants (mirrors reSID's sampling_method enum). */
#define RESID_SAMPLE_FAST        0
#define RESID_SAMPLE_INTERPOLATE 1
#define RESID_SAMPLE_RESAMPLE    2
#define RESID_SAMPLE_RESAMPLE_FASTMEM 3

/*
 * resid_create() - Allocate and initialise a new SID instance.
 * Returns NULL on failure.
 */
resid_sid_t *resid_create(void);

/*
 * resid_destroy() - Free a SID instance previously allocated with
 * resid_create().
 */
void resid_destroy(resid_sid_t *sid);

/*
 * resid_set_chip_model() - Select the chip model to emulate.
 * model: RESID_MOS6581 (default) or RESID_MOS8580
 */
void resid_set_chip_model(resid_sid_t *sid, int model);

/*
 * resid_reset() - Reset the SID chip to power-on state.
 */
void resid_reset(resid_sid_t *sid);

/*
 * resid_read() - Read from a SID register.
 * offset: register offset (0x00–0x1F)
 * Returns the register value.
 */
uint8_t resid_read(resid_sid_t *sid, uint8_t offset);

/*
 * resid_write() - Write to a SID register.
 * offset: register offset (0x00–0x1F)
 * value:  byte value to write
 */
void resid_write(resid_sid_t *sid, uint8_t offset, uint8_t value);

/*
 * resid_set_sampling_parameters() - Configure the sample rate and method.
 *
 * clock_freq:  C64 master clock frequency in Hz (985248 for PAL,
 *              1022727 for NTSC).
 * method:      One of the RESID_SAMPLE_* constants.
 * sample_freq: Desired audio output sample rate in Hz (e.g. 44100).
 * pass_freq:   Low-pass cut-off for the resampling filter, or -1 for default.
 *
 * Returns 1 on success, 0 on failure.
 */
int resid_set_sampling_parameters(resid_sid_t *sid,
                                  double clock_freq,
                                  int method,
                                  double sample_freq,
                                  double pass_freq);

/*
 * resid_clock() - Advance the SID chip by delta_t clock cycles and fill
 * buf with interleaved 16-bit signed PCM samples.
 *
 * delta_t:    Number of clock cycles to emulate.  On return this is set
 *             to the number of cycles that were NOT yet processed (i.e. the
 *             remainder that did not fit in buf).
 * buf:        Output buffer for 16-bit signed PCM samples.
 * buf_samples: Capacity of buf in samples (not bytes).
 *
 * Returns the number of samples written to buf.
 */
int resid_clock(resid_sid_t *sid,
                int *delta_t,
                int16_t *buf,
                int buf_samples);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* RESID_C_H */
