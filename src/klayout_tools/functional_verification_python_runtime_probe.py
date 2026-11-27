"""Measurement taken *inside* the simulator process by klt's Python runtime
probe (issue #8015).

``klt`` itself never imports this module. ``klt functional-verification``
copies it into a scratch directory as ``klt_python_runtime_probe.py``,
compiles an empty Verilog module, and runs this one cocotb test through the
same Icarus launcher, cocotb loading path and environment the request's own
regression is about to use. Running *here*, inside the interpreter ``vvp``
embedded, is the whole point: a helper Python process would report its own
``libpython``, and cocotb's ``Using Python ... interpreter`` banner prints the
compile-time ``PY_VERSION`` of the installed wheel, not the runtime that was
loaded (measured 2026-11-27: banner ``3.14.0`` on a ``cp314`` wheel whose
``vvp`` ran CPython 3.14.9).
"""

from __future__ import annotations

import ctypes
import json
import os

import cocotb

#: Environment variable naming the file the measurement is written to.
RESULT_ENV = "KLT_PYTHON_RUNTIME_PROBE_RESULT"


class _DlInfo(ctypes.Structure):
    """``Dl_info`` as ``dladdr`` fills it (glibc and macOS agree on layout)."""

    _fields_ = [
        ("dli_fname", ctypes.c_char_p),
        ("dli_fbase", ctypes.c_void_p),
        ("dli_sname", ctypes.c_char_p),
        ("dli_saddr", ctypes.c_void_p),
    ]


def supplying_library(address: int | None) -> str | None:
    """Path of the loaded object that contains ``address``, or ``None``.

    ``dladdr`` lives in libc since glibc 2.34 and in ``libdl.so.2`` before
    that; macOS exports it from libSystem, reachable as ``None`` too.
    """
    for name in (None, "libdl.so.2"):
        try:
            dladdr = ctypes.CDLL(name).dladdr
        except (AttributeError, OSError):
            continue
        dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
        dladdr.restype = ctypes.c_int
        info = _DlInfo()
        if dladdr(ctypes.c_void_p(address), ctypes.byref(info)) and info.dli_fname:
            return os.fsdecode(info.dli_fname)
        return None
    return None


@cocotb.test()
async def probe(dut) -> None:
    """Record ``Py_GetVersion()`` and the object supplying that symbol."""
    # ``CDLL(None)`` is the process-global symbol table: the first Py_GetVersion
    # it resolves is, by definition, the one supplying the C-API here.
    api = ctypes.CDLL(None)
    py_get_version = api.Py_GetVersion
    py_get_version.argtypes = []
    py_get_version.restype = ctypes.c_char_p
    address = ctypes.cast(py_get_version, ctypes.c_void_p).value
    with open(os.environ[RESULT_ENV], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "py_get_version": os.fsdecode(py_get_version()),
                "library": supplying_library(address),
            },
            handle,
        )
