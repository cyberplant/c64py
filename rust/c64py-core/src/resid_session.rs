//! Dynamic load of ``resid_c`` (same shared library as Python ``resid.py``).

use libloading::Library;
use std::ffi::c_void;
use std::os::raw::c_int;
use std::path::Path;

type ResidReadFn = unsafe extern "C" fn(*mut c_void, u8) -> u8;
type ResidWriteFn = unsafe extern "C" fn(*mut c_void, u8, u8);
type ResidClockFn =
    unsafe extern "C" fn(*mut c_void, *mut c_int, *mut i16, c_int) -> c_int;

/// One session: loaded ``resid_c`` + SID pointer owned by Python ``ReSIDEmulator``.
///
/// Call only while Python holds ``ReSIDEmulator._lock`` (same as ctypes path).
pub struct ResidSession {
    _lib: Library,
    resid_read: ResidReadFn,
    resid_write: ResidWriteFn,
    resid_clock: ResidClockFn,
    ptr: *mut c_void,
    scratch: Vec<i16>,
    pub pcm: Vec<i16>,
}

impl ResidSession {
    pub fn open(lib_path: &str, ptr: usize) -> Result<Self, String> {
        if ptr == 0 {
            return Err("resid ptr is null".to_string());
        }
        let lib = unsafe {
            Library::new(Path::new(lib_path))
                .map_err(|e| format!("resid_c load {}: {}", lib_path, e))?
        };
        let (resid_read, resid_write, resid_clock) = unsafe {
            let resid_read: ResidReadFn = *lib
                .get(b"resid_read")
                .map_err(|e| format!("resid_read symbol: {}", e))?;
            let resid_write: ResidWriteFn = *lib
                .get(b"resid_write")
                .map_err(|e| format!("resid_write symbol: {}", e))?;
            let resid_clock: ResidClockFn = *lib
                .get(b"resid_clock")
                .map_err(|e| format!("resid_clock symbol: {}", e))?;
            (resid_read, resid_write, resid_clock)
        };
        Ok(Self {
            _lib: lib,
            resid_read,
            resid_write,
            resid_clock,
            ptr: ptr as *mut c_void,
            scratch: vec![0i16; 4096],
            pcm: Vec::new(),
        })
    }

    #[inline]
    pub fn read_reg(&mut self, offset: u8) -> u8 {
        unsafe { (self.resid_read)(self.ptr, offset) }
    }

    #[inline]
    pub fn write_reg(&mut self, offset: u8, value: u8) {
        unsafe {
            (self.resid_write)(self.ptr, offset, value);
        }
    }

    /// Advance reSID by ``cycles`` C64 clocks; append produced PCM (same contract as Python ``tick_cpu_cycles``).
    pub fn clock_cycles(&mut self, mut cycles: i32) {
        if cycles <= 0 {
            return;
        }
        let scratch_n = self.scratch.len() as c_int;
        while cycles > 0 {
            let mut dt = cycles;
            let n = unsafe {
                (self.resid_clock)(
                    self.ptr,
                    &mut dt as *mut c_int,
                    self.scratch.as_mut_ptr(),
                    scratch_n,
                )
            };
            cycles = dt;
            if n > 0 {
                let n = n as usize;
                self.pcm.extend_from_slice(&self.scratch[..n]);
            }
        }
    }

    /// Drain PCM into little-endian bytes for Python.
    pub fn take_pcm_le_bytes(&mut self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.pcm.len() * 2);
        for s in self.pcm.drain(..) {
            out.extend_from_slice(&s.to_le_bytes());
        }
        out
    }
}
