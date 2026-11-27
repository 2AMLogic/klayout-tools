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
import stat
from pathlib import Path

import cocotb


class _DlInfo(ctypes.Structure):
    """``Dl_info`` as ``dladdr`` fills it (glibc and macOS agree on layout)."""

    _fields_ = [
        ("dli_fname", ctypes.c_char_p),
        ("dli_fbase", ctypes.c_void_p),
        ("dli_sname", ctypes.c_char_p),
        ("dli_saddr", ctypes.c_void_p),
    ]


def _mapped_file(
    address: int, maps_path: str = "/proc/self/maps"
) -> tuple[str, int, int] | None:
    """Path, device and inode of the file mapped over the symbol on Linux."""
    try:
        with open(maps_path, encoding="utf-8", errors="surrogateescape") as maps:
            for line in maps:
                fields = line.split(None, 5)
                low, _, high = fields[0].partition("-")
                if int(low, 16) <= address < int(high, 16):
                    inode = int(fields[4])
                    if inode == 0 or len(fields) < 6:
                        return None
                    major, minor = fields[3].split(":")
                    device = os.makedev(int(major, 16), int(minor, 16))
                    return fields[5].rstrip("\n"), device, inode
    except (OSError, ValueError, IndexError):
        pass
    return None


def _dladdr_name(address: int) -> str | None:
    """The loader's name for the object, which may be relative or a symlink."""
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
        return os.fsdecode(info.dli_fname)
    return None


def supplying_library(address: int | None) -> str | None:
    """Report an existing file only if its identity matches the symbol's mapping."""
    if address is None:
        return None
    mapping = _mapped_file(address)
    if mapping is None:
        return None
    mapped_path, device, inode = mapping
    for path in (_dladdr_name(address), mapped_path):
        if path is None or not os.path.isabs(path):
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
            return path
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
    with Path(__file__).with_name("result.json").open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "py_get_version": os.fsdecode(py_get_version()),
                "library": supplying_library(address),
            },
            handle,
        )
