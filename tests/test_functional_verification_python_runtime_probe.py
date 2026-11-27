"""The measurement the Icarus Python runtime probe takes inside ``vvp``
(issue #8015): the shipped probe module run in-process, cocotb's test
registration stubbed. ``test_functional_verification_python_runtime.py``
covers what klt does with the result."""

from __future__ import annotations

import asyncio
import ctypes
import json
import runpy
import subprocess
import sys
import types
from pathlib import Path

import pytest

from klayout_tools import functional_verification_python_runtime as rt

PROBE_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "klayout_tools"
    / rt._PROBE_TEST_SOURCE
)


@pytest.fixture
def probe_module(monkeypatch):
    """Load the shipped probe with only cocotb's test registration stubbed."""
    monkeypatch.setitem(
        sys.modules, "cocotb", types.SimpleNamespace(test=lambda: lambda fn: fn)
    )
    return runpy.run_module(
        "klayout_tools.functional_verification_python_runtime_probe"
    )


def _version_address() -> int:
    return ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p).value


def _fake_dladdr_reporting(name: bytes):
    """A ``CDLL`` stand-in whose ``dladdr`` reports ``name`` as the object's
    file name, as it does for a main program started by a bare name."""

    def dladdr(address, info):
        info._obj.dli_fname = name  # ``info`` is ``ctypes.byref(Dl_info)``
        return 1

    return lambda name_: types.SimpleNamespace(dladdr=dladdr)


def test_probe_records_the_host_runtime(probe_module, tmp_path, monkeypatch):
    result = tmp_path / "result.json"
    monkeypatch.setenv(rt.PROBE_RESULT_ENV, str(result))
    asyncio.run(probe_module["probe"](None))

    measured = json.loads(result.read_text())
    assert measured["py_get_version"] == sys.version
    assert Path(measured["library"]).is_file()


def test_probe_library_lookup_can_be_inconclusive(probe_module, monkeypatch):
    supplying_library = probe_module["supplying_library"]
    assert supplying_library(None) is None  # No mapping contains the null pointer.

    monkeypatch.setattr(ctypes, "CDLL", lambda name: object())  # dladdr unavailable
    assert supplying_library(_version_address()) is None


def test_a_library_path_dladdr_names_is_reported_as_is(probe_module, monkeypatch):
    """The shared-library case: ``dli_fname`` is already an existing absolute
    path, so the kernel's mapping table is not consulted."""
    library = Path(sys.executable).resolve()  # any file will do
    monkeypatch.setattr(ctypes, "CDLL", _fake_dladdr_reporting(bytes(library)))
    lookup = probe_module["supplying_library"]
    monkeypatch.setitem(
        lookup.__globals__,
        "_mapped_file",
        lambda address: pytest.fail("a confirmed name needs no second source"),
    )

    assert lookup(_version_address()) == str(library)


def test_an_executable_found_by_bare_name_is_resolved_through_the_mapping(
    probe_module, tmp_path, monkeypatch
):
    """The executable-backed case: ``dladdr`` returns ``python3`` as the
    program was started, which names no file; the file mapped over the
    address is reported instead."""
    executable = tmp_path / "python3.12"
    executable.write_text("")
    monkeypatch.setattr(ctypes, "CDLL", _fake_dladdr_reporting(b"python3"))
    lookup = probe_module["supplying_library"]
    monkeypatch.setitem(
        lookup.__globals__, "_mapped_file", lambda address: str(executable)
    )

    assert lookup(_version_address()) == str(executable)


@pytest.mark.parametrize(
    "reported,mapped",
    [
        (b"python3", None),  # no mapping table to ask
        (b"python3", "/no/such/dir/python3.12"),  # a name that confirms nothing
        (b"python3", "/usr/bin/python3.12 (deleted)"),  # the file is gone
        (b"/no/such/dir/libpython3.13.so", "[heap]"),  # stale name, no file
    ],
)
def test_a_path_nothing_confirms_is_reported_unresolved(
    probe_module, monkeypatch, reported, mapped
):
    monkeypatch.setattr(ctypes, "CDLL", _fake_dladdr_reporting(reported))
    lookup = probe_module["supplying_library"]
    monkeypatch.setitem(lookup.__globals__, "_mapped_file", lambda address: mapped)

    assert lookup(_version_address()) is None


def test_mapped_file_reads_the_pathname_of_the_mapping_over_an_address(
    probe_module, tmp_path
):
    maps = tmp_path / "maps"
    maps.write_text(
        "00400000-00500000 r-xp 00000000 08:01 1234    /usr/bin/python3.12\n"
        "7f0000000000-7f0000100000 r--p 00000000 08:01 99   /opt/odd dir/libpy.so\n"
        "7f0000100000-7f0000200000 rw-p 00000000 00:00 0\n"
        "7ffc00000000-7ffc00021000 rw-p 00000000 00:00 0                [stack]\n"
    )
    mapped_file = probe_module["_mapped_file"]

    def at(address):
        return mapped_file(address, maps_path=str(maps))

    assert at(0x400000) == "/usr/bin/python3.12"
    assert at(0x4FFFFF) == "/usr/bin/python3.12"
    assert at(0x7F0000000010) == "/opt/odd dir/libpy.so"  # a space survives
    assert at(0x7F0000100010) is None  # anonymous memory has no file
    assert at(0x7FFC00000010) == "[stack]"  # a pseudo-path, never an existing file
    assert at(0x500000) is None  # the end of a range is not inside it
    assert at(0x10) is None  # nothing maps it


@pytest.mark.parametrize("content", ["\n", "not a maps line\n", "zz-yy r-xp\n"])
def test_mapped_file_gives_up_on_a_table_it_cannot_read(
    probe_module, tmp_path, content
):
    maps = tmp_path / "maps"
    maps.write_text(content)
    mapped_file = probe_module["_mapped_file"]

    assert mapped_file(0x400000, maps_path=str(maps)) is None
    assert mapped_file(0x400000, maps_path=str(tmp_path / "absent")) is None


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc/self/maps"
)
def test_an_interpreter_started_by_bare_name_reports_a_file(tmp_path):
    """The case the probe got wrong: an interpreter found through ``PATH`` as
    ``python3``. Run for real in a child started that way, whichever object
    -- the executable, or a shared libpython -- supplies the C-API there."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").symlink_to(sys.executable)
    script = (
        "import runpy, sys, types, ctypes\n"
        "sys.modules['cocotb'] = types.SimpleNamespace(test=lambda: lambda fn: fn)\n"
        "probe = runpy.run_path(sys.argv[1])\n"
        "address = ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p).value\n"
        "print(probe['supplying_library'](address))\n"
    )

    run = subprocess.run(
        ["python3", "-c", script, str(PROBE_SOURCE)],
        env={"PATH": str(tmp_path / "bin")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    library = Path(run.stdout.strip())
    assert library.is_absolute() and library.is_file(), run.stdout
