"""Optional ``sv2v`` SystemVerilog pre-conversion for ``klt synthesize`` (issue #4198).

Yosys' native ``read_verilog -sv`` front end rejects some SystemVerilog
(e.g. unpacked-array module ports). When a request selects
``sv_frontend: "sv2v"`` the sources are converted once, with a single ``sv2v``
invocation, into one Verilog-2005 file that every downstream pass reads in
place of the originals, so all reported numbers describe one elaboration.
There is deliberately no fallback: a missing or failing ``sv2v`` is an error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable

SV_FRONTENDS = ("yosys", "sv2v")
DEFAULT_SV_FRONTEND = "yosys"

_VERSION_TIMEOUT_S = 30
_CONVERT_TIMEOUT_S = 300


def validate_sv_frontend(value: object, error: Callable[[str], Exception]) -> str:
    """The validated ``sv_frontend`` request value (default ``"yosys"``)."""
    if value is None:
        return DEFAULT_SV_FRONTEND
    if value not in SV_FRONTENDS:
        raise error(
            f"request.sv_frontend must be one of {', '.join(SV_FRONTENDS)} "
            f"(got {value!r})"
        )
    return value  # type: ignore[return-value]


def _find_sv2v(error: Callable[[str], Exception]) -> str:
    exe = shutil.which("sv2v")
    if exe is None:
        raise error(
            "sv_frontend 'sv2v' requested but 'sv2v' was not found on PATH; "
            "install the pinned sv2v release (https://github.com/zachjs/sv2v) "
            "or omit request.sv_frontend to use Yosys' native front end"
        )
    return exe


def sv2v_version(exe: str, error: Callable[[str], Exception]) -> str:
    """The ``sv2v --version`` text (first line)."""
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error(f"could not run 'sv2v --version': {exc}") from exc
    text = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 or not text:
        raise error(f"'sv2v --version' failed (exit {proc.returncode}): {text}")
    return text.splitlines()[0]


def convert_sources(
    sources: list[str],
    output_path: str,
    error: Callable[[str], Exception],
    defines: dict[str, str | None] | None = None,
) -> tuple[str, str]:
    """Convert ``sources`` (``.v``/``.sv``/``.svh`` mix) with one ``sv2v`` call
    into ``output_path``. Returns ``(output_path, sv2v_version_text)``.

    Each source's directory is passed as an include directory so ``.svh``
    includes resolve as they would for the native front end.

    ``defines`` are passed as ``--define=NAME[=VALUE]`` in sorted-name order:
    sv2v runs the preprocessor itself, so the later ``read_verilog -D`` sees
    already-preprocessed text and could not apply them.
    """
    exe = _find_sv2v(error)
    version = sv2v_version(exe, error)
    include_dirs: list[str] = []
    for path in sources:
        directory = os.path.dirname(path)
        if directory not in include_dirs:
            include_dirs.append(directory)
    # ``.svh`` files are include-only; sv2v reads them through `include.
    inputs = [p for p in sources if not p.lower().endswith(".svh")]
    cmd = [exe]
    for directory in include_dirs:
        cmd.append(f"--incdir={directory}")
    for name in sorted(defines or {}):
        value = (defines or {})[name]
        cmd.append(f"--define={name}" if value is None else f"--define={name}={value}")
    cmd += ["-w", output_path, *inputs]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CONVERT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error(f"sv2v conversion failed to run: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(output_path):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise error(f"sv2v conversion failed (exit {proc.returncode}): {detail}")
    return output_path, version
