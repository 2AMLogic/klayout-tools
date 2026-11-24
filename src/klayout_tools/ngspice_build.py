"""Build and version identity shared by local ngspice consumers."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from ._digest import sha256_file

# Keep local and worker version-banner capture bounds identical.
ENGINE_BANNER_MAX = 2000
_ENGINE_BANNER_TIMEOUT_S = 10
_ENGINE_VERSION_RE = re.compile(r"ngspice-([\w.]+)")


def probe_engine_build(ngspice_binary: str | None) -> dict[str, Any] | None:
    """The local ngspice build identity (issue #3095) -- ``--version`` banner plus
    binary sha256 -- probed once per sweep into ``environment.engine_build`` on
    ``local`` reports, ``client_engine_build`` off-host (#5566 rewrites ``engine_build``
    to the worker's). ``engine_version`` is the major version only and two builds of one
    major can differ in options (FFTW vs. padded FFT). ``None`` for engines without a
    probe (xyce). A failed probe never fails the run; ``probe`` / ``reason`` say why."""
    if ngspice_binary is None:
        return None
    build: dict[str, Any] = {
        "version_banner": None,
        "binary_sha256": None,
        "probe": "ok",
        "reason": None,
    }
    try:
        build["binary_sha256"] = sha256_file(ngspice_binary)
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [ngspice_binary, "--version"],
            capture_output=True,
            text=False,
            timeout=_ENGINE_BANNER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        build.update(
            probe="timeout", reason=f"--version exceeded {_ENGINE_BANNER_TIMEOUT_S}s"
        )
        return build
    except Exception as exc:  # the probe must never fail the sweep
        build.update(probe="error", reason=f"could not run --version: {exc}"[:200])
        return build
    raw = proc.stdout or proc.stderr or b""
    text: str = (
        raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    )[: ENGINE_BANNER_MAX * 4]
    banner = "".join(ch if (ch >= " " or ch in "\n\t") else "?" for ch in text).strip()[
        :ENGINE_BANNER_MAX
    ]
    if proc.returncode != 0 or not banner:
        build.update(
            probe="error",
            reason=(
                f"--version exited {proc.returncode}"
                if proc.returncode != 0
                else "--version printed no banner"
            ),
        )
        return build
    build["version_banner"] = banner
    return build


def engine_version(stdout: str) -> str | None:
    match = _ENGINE_VERSION_RE.search(stdout or "")
    return match.group(1) if match else None
