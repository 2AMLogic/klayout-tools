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


def _is_file(path: str | None) -> bool:
    return path is not None and os.path.isabs(path) and os.path.isfile(path)


def _mapped_file(address: int, maps_path: str = "/proc/self/maps") -> str | None:
    """File the kernel reports mapped over ``address`` (Linux
    ``/proc/self/maps``), or ``None`` where that cannot be read."""
    try:
        with open(maps_path, encoding="utf-8", errors="surrogateescape") as maps:
            for line in maps:
                fields = line.split(None, 5)
                low, _, high = fields[0].partition("-")
                if int(low, 16) <= address < int(high, 16):
                    return fields[5].rstrip("\n") if len(fields) > 5 else None
    except (OSError, ValueError, IndexError):
        pass
    return None


def supplying_library(address: int | None) -> str | None:
    """Path of the loaded object that contains ``address``, or ``None`` when
    no existing file can be confirmed as that object.

    ``dladdr`` lives in libc since glibc 2.34 and in ``libdl.so.2`` before
    that; macOS exports it from libSystem, reachable as ``None`` too. Its
    ``dli_fname`` is a library's path, but for the main program it is the name
    the program was started under -- a bare ``python3`` found through
    ``PATH`` -- which names no file. A name that is not an existing absolute
    path is therefore replaced by the file the kernel says is mapped there,
    and reported unresolved when neither confirms.
    """
    if address is None:
        return None
    for name in (None, "libdl.so.2"):
        try:
            dladdr = ctypes.CDLL(name).dladdr
        except (AttributeError, OSError):
            continue
        dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
        dladdr.restype = ctypes.c_int
        info = _DlInfo()
        if not (
            dladdr(ctypes.c_void_p(address), ctypes.byref(info)) and info.dli_fname
        ):
            return None
        reported = os.fsdecode(info.dli_fname)
        if _is_file(reported):
            return reported
        mapped = _mapped_file(address)
        return mapped if _is_file(mapped) else None
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
