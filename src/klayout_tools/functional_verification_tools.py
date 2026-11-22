"""Version probes and digest checks for functional-verification host tools."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any

from ._provenance import sha256_file

_ENGINE_VERSION_COMMANDS = {
    "icarus": (["iverilog", "-V"], re.compile(r"Icarus Verilog version (\S+)")),
    "verilator": (["verilator", "--version"], re.compile(r"Verilator (\S+)")),
}
_TOOL_VERSION_ARGS = {argv[0]: argv[1] for argv, _ in _ENGINE_VERSION_COMMANDS.values()}


def _check_required_tool(
    name: str,
    want_version: str | None,
    want_sha256: str | None,
    search_path: str | None,
    run_env: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve one tool; return ``(tool_echo, unmet)``. ``tool_echo`` is
    ``None`` when the binary was not found; ``unmet`` is ``None`` when every
    requested constraint holds."""
    resolved = shutil.which(name, path=search_path)
    if resolved is None:
        missing: dict[str, Any] = {
            "name": name,
            "wanted": "on PATH",
            "found": None,
            "reason": "not found",
        }
        return None, missing
    version_arg = _TOOL_VERSION_ARGS.get(os.path.basename(resolved), "--version")
    observed = _tool_version_output(resolved, run_env, version_arg)
    try:
        digest = sha256_file(resolved)
    except OSError:
        digest = None
    tool: dict[str, Any] = {
        "name": name,
        "path": resolved,
        "version": observed,
        "sha256": digest,
    }
    problems = []
    if want_version is not None and (observed is None or want_version not in observed):
        problems.append(f"version {want_version!r} not in {version_arg} output")
    if want_sha256 is not None and digest != want_sha256.lower():
        problems.append(f"sha256 {want_sha256} != {digest}")
    if not problems:
        return tool, None
    unmet: dict[str, Any] = {
        "name": name,
        "wanted": {"version": want_version, "sha256": want_sha256},
        "found": {"path": resolved, "version": observed, "sha256": digest},
        "reason": "; ".join(problems),
    }
    return tool, unmet


def _tool_version_output(
    path: str, env: dict[str, str], version_arg: str
) -> str | None:
    """First non-empty line of ``<path> <version_arg>`` (stdout, else stderr);
    ``None`` if it cannot run. A non-zero exit still counts when it printed."""
    try:
        proc = subprocess.run(
            [path, version_arg],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return None
