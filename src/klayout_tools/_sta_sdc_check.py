"""Opt-in SDC drift check for ``klt sta`` (issue #5236).

A flow that keeps its constraints in a committed ``.sdc`` must duplicate them
into the ``klt sta`` request, and nothing noticed the two diverging. With
``request.sdc_check = {"path": "<file>.sdc"}`` this module reads the file with a
small **pure-Python** reader -- no Tcl is executed, nothing reaches OpenSTA, the
SDC is never a constraint source -- and compares the four supported commands
(``create_clock``, ``set_input_delay``, ``set_output_delay``,
``set_clock_uncertainty``) with the request's constraints. Every other command
(and any supported command written in a form the reader does not understand) is
listed by name in ``unchecked``; it is never silently passed.

Drift is data, not a gate: the response block is informational and the exit
code is unaffected. Split out of ``post_route_sta.py`` (per-file size ratchet,
issue #2766); errors are ``PostRouteStaError``, imported lazily.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any

from . import env_provenance

_TOLERANCE = 1e-9
_TOKEN = re.compile(r"\[[^\]]*\]|\S+")
_SUPPORTED = (
    "create_clock",
    "set_input_delay",
    "set_output_delay",
    "set_clock_uncertainty",
)


def _error(message: str) -> Exception:
    from .post_route_sta import PostRouteStaError

    return PostRouteStaError(message)


def resolve_sdc_check(field: Any, request_dir: str) -> str | None:
    """Validate ``request.sdc_check`` and return the absolute SDC path
    (``None`` when the field is omitted/null)."""
    if field is None:
        return None
    if not isinstance(field, dict) or set(field) != {"path"}:
        raise _error('request.sdc_check must be an object {"path": "<file>.sdc"}')
    given = field["path"]
    if not isinstance(given, str) or not given:
        raise _error("request.sdc_check.path must be a non-empty string")
    path = given if os.path.isabs(given) else os.path.join(request_dir, given)
    if not os.path.isfile(path):
        raise _error(f"sdc_check file not found: {given}")
    return os.path.abspath(path)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first line number, text)`` per command, joining ``\\`` continuations
    and dropping blank lines and comments."""
    out: list[tuple[int, str]] = []
    pending, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not pending:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        full = (pending + line).strip()
        pending = ""
        if full and not full.startswith("#"):
            out.append((start, full))
    if pending.strip() and not pending.strip().startswith("#"):
        out.append((start, pending.strip()))
    return out


def _num(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _names(token: str, getter: str) -> str | None:
    """A single port/clock name from ``[getter X]`` or a bare word."""
    if token.startswith("["):
        words = token[1:-1].split()
        return words[1] if len(words) == 2 and words[0] == getter else None
    return None if any(c in token for c in '{}$;"') else token


def _parse_command(words: list[str]) -> dict[str, Any] | None:
    """One supported command as a record, or ``None`` if its form is not
    understood (the caller then lists it as unchecked)."""
    cmd, args = words[0], words[1:]
    flags: dict[str, str | bool] = {}
    positional: list[str] = []
    valued = {
        "create_clock": {"-name", "-period"},
        "set_input_delay": {"-clock"},
        "set_output_delay": {"-clock"},
        "set_clock_uncertainty": set(),
    }[cmd]
    boolean = {"-min", "-max", "-setup", "-hold"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in valued:
            if i + 1 >= len(args):
                return None
            flags[arg] = args[i + 1]
            i += 2
        elif arg in boolean and cmd != "create_clock":
            flags[arg] = True
            i += 1
        elif arg.startswith("-") and _num(arg) is None:
            return None  # -add_delay, -waveform, -rise, ... : not checked
        else:
            positional.append(arg)
            i += 1
    if cmd == "create_clock":
        period = _num(str(flags.get("-period", "")))
        if period is None or len(positional) != 1:
            return None
        port = _names(positional[0], "get_ports")
        name = flags.get("-name", port)
        if port is None or not isinstance(name, str):
            return None
        return {"cmd": cmd, "subject": name, "value": period}
    if cmd == "set_clock_uncertainty":
        if len(positional) not in (1, 2) or ("-setup" in flags and "-hold" in flags):
            return None
        value = _num(positional[0])
        clock = _names(positional[1], "get_clocks") if len(positional) == 2 else None
        if value is None or (len(positional) == 2 and clock is None):
            return None
        sides = (
            ["setup"]
            if "-setup" in flags
            else ["hold"]
            if "-hold" in flags
            else ["setup", "hold"]
        )
        return {"cmd": cmd, "subject": clock, "value": value, "sides": sides}
    # set_input_delay / set_output_delay
    if len(positional) != 2 or not isinstance(flags.get("-clock"), str):
        return None
    value = _num(positional[0])
    target = positional[1]
    all_token = "[all_inputs]" if cmd == "set_input_delay" else "[all_outputs]"
    port = None if target == all_token else _names(target, "get_ports")
    if value is None or (port is None and target != all_token):
        return None
    sides = (
        ["min"] if "-min" in flags else ["max"] if "-max" in flags else ["min", "max"]
    )
    return {
        "cmd": cmd,
        "subject": port,  # None == every port of that direction
        "clock": flags["-clock"],
        "value": value,
        "sides": sides,
    }


def parse_sdc(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(supported commands, unchecked entries)`` for the SDC text."""
    commands: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    for line_no, line in _logical_lines(text):
        words = _TOKEN.findall(line)
        parsed = None
        reason = "command not in the checked subset"
        if words[0] in _SUPPORTED:
            reason = "unsupported option or argument form"
            if ";" not in line and "{" not in line and "$" not in line:
                parsed = _parse_command(words)
        if parsed is None:
            unchecked.append({"command": words[0], "line": line_no, "reason": reason})
        else:
            parsed["line"] = line_no
            commands.append(parsed)
    return commands, unchecked


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= _TOLERANCE


def _mismatch(
    command: str, subject: Any, sdc: Any, request: Any, **extra: Any
) -> dict[str, Any]:
    return {
        "command": command,
        "subject": subject,
        "sdc_value": sdc,
        "request_value": request,
        **extra,
    }


def _request_delay(
    scalar: float | None,
    per_port: dict[str, dict[str, float]] | None,
    port: str,
    side: str,
) -> float | None:
    entry = (per_port or {}).get(port)
    if entry is not None:
        return entry.get(f"{side}_ns")
    return scalar


def _compare_clocks(commands, clocks, out):
    sdc_by_name: dict[str, float] = {}
    for c in commands:
        if c["cmd"] != "create_clock":
            continue
        sdc_by_name[c["subject"]] = c["value"]
        req = next((k for k in clocks if k["port"] == c["subject"]), None)
        period = req["period_ns"] if req else None
        accepted = list(period.values()) if isinstance(period, dict) else [period]
        if not any(_same(c["value"], p) for p in accepted):
            out.append(
                _mismatch(
                    "create_clock", c["subject"], c["value"], period, line=c["line"]
                )
            )
    for k in clocks:
        if k["port"] not in sdc_by_name:
            out.append(_mismatch("create_clock", k["port"], None, k["period_ns"]))


def _compare_delays(commands, cmd, scalar, per_port, clock_port, out):
    seen: dict[tuple[str | None, str], float] = {}
    for c in commands:
        if c["cmd"] != cmd:
            continue
        for side in c["sides"]:
            seen[(c["subject"], side)] = c["value"]
            if c["subject"] is None:
                request = scalar
            else:
                request = _request_delay(scalar, per_port, c["subject"], side)
            if not _same(c["value"], request):
                out.append(
                    _mismatch(
                        cmd,
                        c["subject"] or "*",
                        c["value"],
                        request,
                        bound=side,
                        line=c["line"],
                    )
                )
    for side in ("min", "max"):
        if scalar is not None and (None, side) not in seen:
            out.append(_mismatch(cmd, "*", None, scalar, bound=side))
    for port, bounds in sorted((per_port or {}).items()):
        for side in ("min", "max"):
            want = bounds.get(f"{side}_ns")
            if want is None:
                continue
            got = seen.get((port, side), seen.get((None, side)))
            if not _same(got, want):
                out.append(_mismatch(cmd, port, got, want, bound=side))


def _compare_uncertainty(commands, uncertainty, clock_port, out):
    seen: set[str] = set()
    for c in commands:
        if c["cmd"] != "set_clock_uncertainty":
            continue
        for side in c["sides"]:
            subject = c["subject"] or clock_port
            request = (
                (uncertainty or {}).get(f"{side}_ns") if subject == clock_port else None
            )
            if subject == clock_port:
                seen.add(side)
            if not _same(c["value"], request):
                out.append(
                    _mismatch(
                        "set_clock_uncertainty",
                        subject,
                        c["value"],
                        request,
                        bound=side,
                        line=c["line"],
                    )
                )
    for side in ("setup", "hold"):
        value = (uncertainty or {}).get(f"{side}_ns")
        if value is not None and side not in seen:
            out.append(
                _mismatch("set_clock_uncertainty", clock_port, None, value, bound=side)
            )


def sdc_drift_block(
    sdc_path: str,
    *,
    request_dir: str,
    clocks: list[dict[str, Any]],
    clock_port: str,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
    clock_uncertainty: dict[str, float | None] | None,
) -> dict[str, Any]:
    """The response's ``sdc_drift`` block: file identity + hash, each
    mismatch, every unchecked command, and ``status`` (``match``/``drift``)."""
    try:
        with open(sdc_path, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _error(
            f"could not read sdc_check file '{os.path.basename(sdc_path)}': {exc}"
        ) from exc
    commands, unchecked = parse_sdc(text)
    mismatches: list[dict[str, Any]] = []
    _compare_clocks(commands, clocks, mismatches)
    _compare_delays(
        commands,
        "set_input_delay",
        input_delay_ns,
        input_delays,
        clock_port,
        mismatches,
    )
    _compare_delays(
        commands,
        "set_output_delay",
        output_delay_ns,
        output_delays,
        clock_port,
        mismatches,
    )
    _compare_uncertainty(commands, clock_uncertainty, clock_port, mismatches)
    envelope = env_provenance.repo_relative_path(
        sdc_path, repo_root=env_provenance.find_repo_root(request_dir)
    )
    return {
        "path": {
            **envelope,
            "content_hash": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        },
        "mismatches": mismatches,
        "unchecked": unchecked,
        "status": "drift" if mismatches else "match",
    }
