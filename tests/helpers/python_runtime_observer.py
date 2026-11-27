"""A disposable cocotb test that observes its own ``vvp`` process (issue #8015).

Copied into a scratch directory by
``tests/test_functional_verification_python_runtime_live.py`` and run through
``klt functional-verification`` like any testbench. From inside the simulator
it records what the process-global symbol table resolves ``Py_GetVersion`` to
and -- independently of the ``dladdr`` the production probe uses, from
``/proc/self/maps`` on Linux -- which file is mapped across that address.

Never imported by pytest (it needs cocotb and a simulator-injected ``dut``);
``cocotb`` is therefore imported at module scope on purpose.
"""

import ctypes
import json
import os

import cocotb


def _mapped_across(address: int) -> str | None:
    """The mapped file whose range contains ``address``, or ``None`` where
    there is no ``/proc/self/maps`` (non-Linux) or none does."""
    try:
        with open("/proc/self/maps", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split(maxsplit=5)
        start, end = (int(part, 16) for part in fields[0].partition("-")[::2])
        if start <= address < end and len(fields) == 6:
            return os.path.realpath(fields[5].strip())
    return None


@cocotb.test()
async def observe(dut) -> None:
    """Write ``{"version": ..., "mapped": ...}`` for the test to compare."""
    function = ctypes.CDLL(None).Py_GetVersion
    function.restype = ctypes.c_char_p
    address = ctypes.cast(function, ctypes.c_void_p).value
    with open(os.environ["OBSERVATION"], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": os.fsdecode(function()),
                "mapped": _mapped_across(address or 0),
            },
            handle,
        )
