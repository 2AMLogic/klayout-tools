"""Standalone KLayout runset launcher shared by the LVS and extract engines."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from ._paths import resolve_tool_binary
from ._provenance import sha256_file

KLAYOUT_BINARY_ENV_VAR = "KLT_KLAYOUT_BINARY"
_ERROR_LINE_RE = re.compile(r"^\s*ERROR\b", re.IGNORECASE)


def resolve_path(path: str, base_dir: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path))
    return expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)


def sha_prefixed(path: str | None) -> str | None:
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def resolve_klayout_binary(
    options: dict[str, Any], request_dir: str, *, error_cls: type[Exception]
) -> str:
    """Locate the standalone KLayout application for a runset request."""
    resolved = resolve_tool_binary(
        "klayout",
        options,
        option_key="klayout_binary",
        option_label="options.klayout_binary",
        env_var=KLAYOUT_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=("klayout",),
        error_cls=error_cls,
        install_hint=(
            "Install the standalone KLayout application "
            "(https://www.klayout.de/build.html) -- the pip `klayout` package "
            "cannot run a PDK runset -- or use engine 'klayout' instead."
        ),
    )
    assert resolved is not None
    return resolved


def run_runset(
    *,
    binary: str,
    deck_file: str,
    layout: str,
    reference: str,
    top: str | None,
    deck_vars: dict[str, str],
    timeout_s: float,
    work_dir: str,
    error_cls: type[Exception],
    timeout_knob: str = "options.runset_timeout_s",
) -> tuple[str, subprocess.CompletedProcess[str]]:
    """Return the database path and process result, including non-zero exits
    that produced a database. Raise the caller's error on launch failure,
    timeout, or a missing database.
    """

    # cwd=work_dir below: path arguments must not depend on klt's own cwd.
    binary = os.path.abspath(binary) if os.sep in binary else binary
    deck_file, layout, reference = (
        os.path.abspath(p) for p in (deck_file, layout, reference)
    )
    report_path = os.path.join(work_dir, "report.lvsdb")
    cmd = [
        binary,
        "-b",
        "-r",
        deck_file,
        "-rd",
        f"input={layout}",
        "-rd",
        f"schematic={reference}",
        "-rd",
        f"report={report_path}",
    ]
    if top:
        cmd.extend(["-rd", f"top={top}", "-rd", f"topcell={top}"])
    for name, value in deck_vars.items():
        cmd.extend(["-rd", f"{name}={value}"])
    try:
        # cwd=work_dir catches only cwd-relative side files, not where
        # ``<top>_extracted.cir`` lands (#5481; see extract_runset.py).
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, cwd=work_dir
        )
    except FileNotFoundError as exc:
        raise error_cls(
            f"could not launch klayout: '{binary}' could not be executed ({exc})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise error_cls(
            f"the LVS runset did not complete within {timeout_s}s (raise "
            f"{timeout_knob} to allow more time)"
        ) from exc

    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0 and not os.path.isfile(report_path):
        raise error_cls(nonzero_exit_message(binary, completed))
    if not os.path.isfile(report_path):
        raise error_cls(
            "the LVS runset did not write an .lvsdb database -- it likely "
            "failed before comparing (klayout can exit 0 in that case). "
            f"klayout's own output:\n{output.strip()}"
        )
    return report_path, completed


def nonzero_exit_message(
    binary: str, completed: subprocess.CompletedProcess[str]
) -> str:
    """Error text for a runset that exited non-zero without a usable verdict."""
    output = (completed.stdout or "") + (completed.stderr or "")
    errors = error_lines(output)
    cause = f"last error: {errors[-1]}\n" if errors else ""
    app_version = application_version(binary) or "unknown"
    return (
        f"{cause}klayout exited with status {completed.returncode} while "
        f"running the LVS runset -- no trustworthy verdict (klayout "
        f"application {app_version}, pip klayout module "
        f"{module_version()}). klayout's own output:\n{output.strip()}"
    )


def error_lines(output: str) -> list[str]:
    return [ln.strip() for ln in output.splitlines() if _ERROR_LINE_RE.match(ln)]


def module_version() -> str:
    """Version of the pip ``klayout`` module (``unknown`` if unreadable)."""
    from importlib import metadata

    try:
        return metadata.version("klayout")
    except metadata.PackageNotFoundError:
        return "unknown"


def application_version(binary: str) -> str | None:
    """Version string of the standalone application (``klayout -v``)."""
    try:
        out = subprocess.run(
            [binary, "-v"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", out or "")
    return match.group(1) if match else None
