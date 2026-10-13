"""Shared native-extension loader used across the ``klt`` verb modules.

``yield_sensitivity.py``, ``yield_analysis.py``, ``congestion.py``,
``mom.py``, and ``sta.py`` each defined their own near-identical
``_load_native()`` -- a ``try: import klt_<x>_native / except ImportError:
raise <X>Error(...)`` shape, differing only in the native module's name,
the ``*Error`` class raised, the ``native/<x>/`` build directory, the
alternate install command mentioned in the hint, and (for two of the five)
a trailing docs link. ``_load_native_extension`` below factors that literal
prefix out the same way ``_paths.py``'s ``_load_request_json`` /
``_load_spec_json`` do for the request/spec-loading duplication -- each
caller still owns its own ``_load_native()`` wrapper (so call sites and
existing test mocks/patches targeting ``<module>._load_native`` do not need
to change) and just forwards to this helper with its own parameters.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import shutil
import subprocess
from typing import Any


def _load_native_extension(
    module_name: str,
    error_cls: type[Exception],
    build_dir: str,
    install_hint: str,
    docs_link: str | None = None,
) -> Any:
    """Import ``module_name`` (a native Rust extension), raising
    ``error_cls`` with a build-instructions message if it is not installed.

    ``build_dir`` is the ``native/<x>/`` directory ``maturin develop
    --release`` should be run from; ``install_hint`` is the alternate
    install command (e.g. ``uv sync --group yield`` or ``pip install
    ./native/mom``) quoted in the message's ``(or ...)`` clause;
    ``docs_link`` -- when given -- is appended as a trailing ``; see
    <docs_link>``, matching the two callers whose message links a docs page.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        message = (
            f"the {module_name} extension is not installed -- from a repo "
            f"checkout, run `maturin develop --release` inside {build_dir} "
            f"(or `{install_hint}`)"
        )
        if docs_link is not None:
            message += f"; see {docs_link}"
        raise error_cls(message) from exc


# --------------------------------------------------------------------------- #
# Cargo-built native binaries (`klt-techmap`, `klt-wave`) -- issue #3189
# --------------------------------------------------------------------------- #


@functools.cache
def _cargo_reported_target_dir(crate_dir: str) -> str | None:
    """The target directory cargo itself would build ``crate_dir`` into,
    from ``cargo metadata`` -- the only way to honour a ``build.target-dir``
    set in a cargo config file. ``None`` when cargo is absent or cannot
    answer. Cached per crate; this only reads metadata, it never builds."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return None
    try:
        completed = subprocess.run(
            [cargo, "metadata", "--no-deps", "--format-version", "1", "--offline"],
            cwd=crate_dir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        target = json.loads(completed.stdout).get("target_directory")
    except (json.JSONDecodeError, AttributeError):
        return None
    return target if isinstance(target, str) and target else None


def _cargo_target_dirs(crate_dir: str) -> list[str]:
    """Candidate target directories for ``crate_dir``, cheapest first: the
    crate-local ``target/``, then ``$CARGO_TARGET_DIR`` (relative to the
    crate, as cargo resolves it). Cargo's own answer is consulted
    separately, only when these miss."""
    dirs = [os.path.join(crate_dir, "target")]
    env_dir = os.environ.get("CARGO_TARGET_DIR")
    if env_dir:
        dirs.append(os.path.normpath(os.path.join(crate_dir, env_dir)))
    return dirs


def _binary_in(target_dir: str, name: str) -> str | None:
    for profile in ("release", "debug"):
        candidate = os.path.join(target_dir, profile, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def find_cargo_binary(crate_dir: str, name: str) -> str | None:
    """The built ``name`` binary for the cargo crate at ``crate_dir``, or
    ``None`` -- release preferred over debug in each target directory.

    Cargo does not always build into ``<crate>/target``: ``CARGO_TARGET_DIR``
    or a config file's ``build.target-dir`` redirects it (common on hosts
    that share one target dir across projects), so a hardcoded
    ``<crate>/target/...`` reported a built binary as missing (#3189).
    Never runs ``cargo build`` -- building stays a deliberate step.
    """
    seen: set[str] = set()
    for target_dir in _cargo_target_dirs(crate_dir):
        seen.add(target_dir)
        found = _binary_in(target_dir, name)
        if found is not None:
            return found
    reported = _cargo_reported_target_dir(crate_dir)
    if reported is not None and os.path.normpath(reported) not in seen:
        return _binary_in(reported, name)
    return None


def build_cargo_binary(crate_dir: str, name: str, *, release: bool = False) -> str:
    """Build the crate at ``crate_dir`` and return the path cargo reports for
    the ``name`` executable -- wherever its target directory is. For tests
    and benchmark scripts, never production code paths. Raises
    ``RuntimeError`` if the build fails or reports no such executable."""
    command = ["cargo", "build", "--message-format=json-render-diagnostics"]
    if release:
        command.append("--release")
    completed = subprocess.run(
        command, cwd=crate_dir, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cargo build failed in {crate_dir}:\n{completed.stderr[-4000:]}"
        )
    executable = None
    for line in completed.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == name
            and message.get("executable")
        ):
            executable = message["executable"]
    if executable is None or not os.path.isfile(executable):
        raise RuntimeError(
            f"cargo build in {crate_dir} produced no `{name}` executable"
        )
    return executable
