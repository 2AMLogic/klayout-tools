"""The measurement the Icarus Python runtime probe takes inside ``vvp``
(issue #8015): the shipped probe module run in-process, cocotb's test
registration stubbed. ``test_functional_verification_python_runtime.py``
covers what klt does with the result."""

from __future__ import annotations

import asyncio
import ctypes
import json
import mmap
import os
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
    monkeypatch.setitem(
        probe_module["probe"].__globals__, "__file__", str(tmp_path / "probe.py")
    )
    asyncio.run(probe_module["probe"](None))

    measured = json.loads(result.read_text())
    assert measured["py_get_version"] == sys.version
    if Path("/proc/self/maps").is_file():
        assert Path(measured["library"]).is_file()
    else:
        assert measured["library"] is None


def test_probe_library_lookup_can_be_inconclusive(probe_module, monkeypatch):
    assert probe_module["supplying_library"](None) is None
    monkeypatch.setattr(ctypes, "CDLL", lambda name: object())  # dladdr unavailable
    assert probe_module["_dladdr_name"](_version_address()) is None


@pytest.fixture
def loaded_library(probe_module, monkeypatch, tmp_path):
    """Capture the kernel's file identity before any paths change."""
    library = tmp_path / "libpython.so"
    library.touch()
    info = library.stat()
    lookup = probe_module["supplying_library"]
    mapping = str(library), info.st_dev, info.st_ino
    monkeypatch.setitem(lookup.__globals__, "_mapped_file", lambda address: mapping)
    return library, lookup


@pytest.mark.skipif(
    not Path("/proc/self/maps").is_file(), reason="reads /proc/self/maps"
)
def test_a_retargeted_loader_symlink_resolves_to_the_mapped_file(
    probe_module, monkeypatch, tmp_path
):
    library = tmp_path / "loaded.so"
    library.write_bytes(b"x")
    link = tmp_path / "loader-name.so"
    link.symlink_to(library)
    monkeypatch.setattr(ctypes, "CDLL", _fake_dladdr_reporting(bytes(link)))
    lookup = probe_module["supplying_library"]
    with link.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as mapping:
        address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
        assert Path(lookup(address)).samefile(library)

        replacement = tmp_path / "replacement.so"
        replacement.touch()
        link.unlink()
        link.symlink_to(replacement)
        assert lookup(address) == str(library)

        # Both names now resolve to the replacement; the original is still mapped.
        replacement.replace(library)
        link.unlink()
        link.symlink_to(library)
        assert lookup(address) is None


def test_a_matching_inode_on_a_different_device_is_unresolved(
    loaded_library, monkeypatch
):
    library, lookup = loaded_library
    monkeypatch.setattr(ctypes, "CDLL", _fake_dladdr_reporting(bytes(library)))
    info = library.stat()
    monkeypatch.setitem(
        lookup.__globals__,
        "_mapped_file",
        lambda address: (str(library), info.st_dev + 1, info.st_ino),
    )
    assert lookup(_version_address()) is None


def test_an_executable_found_by_bare_name_is_resolved_through_the_mapping(
    loaded_library, monkeypatch
):
    library, lookup = loaded_library
    monkeypatch.setattr(ctypes, "CDLL", _fake_dladdr_reporting(b"python3"))
    assert lookup(_version_address()) == str(library)


def test_an_absolute_name_without_a_mapping_is_unresolved(probe_module, monkeypatch):
    monkeypatch.setattr(
        ctypes, "CDLL", _fake_dladdr_reporting(bytes(Path(sys.executable)))
    )
    lookup = probe_module["supplying_library"]
    monkeypatch.setitem(lookup.__globals__, "_mapped_file", lambda address: None)
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

    assert at(0x400000) == ("/usr/bin/python3.12", os.makedev(8, 1), 1234)
    assert at(0x4FFFFF) == at(0x400000)
    assert at(0x7F0000000010) == ("/opt/odd dir/libpy.so", os.makedev(8, 1), 99)
    assert at(0x7F0000100010) is None  # anonymous memory has no file
    assert at(0x7FFC00000010) is None  # a pseudo-path has no file identity
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
