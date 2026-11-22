"""``analysis.post_commands`` for ``klt sim`` (issue #6080).

A waveform-transform control command (``linearize``, ``fft``) has to run
*between* the transient and the ``measurements[].expr`` lets: after ``fft`` the
current plot is the spectrum, so the ``expr`` entries read spectrum bins.
``analysis.post_commands`` declares that sequence on a scalar ``tran``::

    {"analysis": {"kind": "tran", "args": "1u 2m",
                  "post_commands": ["linearize v(a) v(b)", "fft v(a) v(b)"]}}

The lines are emitted in order after the analysis line, after the rawfile
``write`` and the ``linearize`` flag, and before the ``expr`` lets. Each entry
selects vectors for ``linearize`` or waveform expressions for ``fft``.

Control lines other than ``expr``/``spice`` measurements (crossings, energy,
eye, ...) assume the transient plot is current, which stops being true after
``fft``; such measurement kinds are refused together with ``post_commands``.

``analysis`` is already part of the resume fingerprint and is forwarded to
off-host workers verbatim, so a request without the field is byte-identical to
before; a runner that predates the field is gated in
``sim_batch_contract_spec._RUNNER_REQUEST_FEATURES``.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_errors import SimError

#: First tokens a ``post_commands`` entry may start with.
ALLOWED_COMMANDS = ("linearize", "fft")


# Keep the grammar in the ngspice expression domain. In particular, match a
# complete vector reference before considering a bare name or a numeric scale.
_NAME = r"[a-z_][a-z0-9_.:]*"
_NODE = rf"(?:{_NAME}|[0-9]+)"
_VECTOR = rf"(?:[vi]\({_NODE}\)|@{_NAME}\[{_NAME}\]|{_NAME}(?:#branch)?)"
_FLAGS = re.ASCII | re.IGNORECASE
_SPACE = re.compile(r"[ \t]+")
_LINEAR_VECTOR = re.compile(rf"{_VECTOR}|[0-9]+", _FLAGS)
_NUMBER = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?(?:meg|mil|[tgkmunpf])?"
_FFT_TERM = re.compile(
    rf"[ \t]*(?:-[ \t]*)?(?:(?P<group>(?:mag|abs|real|imag|db|ph)?\()"
    rf"|v\({_NODE},[ \t]*{_NODE}\)|{_VECTOR}|{_NUMBER})",
    _FLAGS,
)
_FFT_OP = re.compile(r"[ \t]*[-+*/^%]")
_END_GROUP = re.compile(r"[ \t]*\)")


def _fft_expression_end(args: str, start: int) -> int | None:
    """Consume a waveform expression, leaving the next list separator intact."""
    depth = 0
    pos = start
    while term := _FFT_TERM.match(args, pos):
        pos = term.end()
        if term["group"]:
            depth += 1
            continue
        while depth and (close := _END_GROUP.match(args, pos)):
            depth -= 1
            pos = close.end()
        operator = _FFT_OP.match(args, pos)
        if operator is None:
            return None if depth else pos
        pos = operator.end()
    return None


def _valid_arguments(verb: str, args: str) -> bool:
    if verb == "linearize":
        return not args or all(_LINEAR_VECTOR.fullmatch(v) for v in _SPACE.split(args))
    start = 0
    while start < len(args):
        end = _fft_expression_end(args, start)
        if end is None:
            return False
        if end == len(args):
            return True
        separator = _SPACE.match(args, end)
        if separator is None:
            return False
        start = separator.end()
        # A group immediately after a name can be interpreted as a function call.
        if args.startswith("(", start):
            return False
    return False


def is_declared(analysis: Any) -> bool:
    return isinstance(analysis, dict) and "post_commands" in analysis


def reject_in_steps(request: dict[str, Any]) -> None:
    """``linearize`` (issue #4014) and ``post_commands`` (issue #6080) are
    scalar-``tran``-only fields."""
    holders = [("analysis", request.get("analysis"))]
    holders.extend(
        (f"analysis_steps[{i}]", step)
        for i, step in enumerate(request.get("analysis_steps") or [])
    )
    for where, holder in holders:
        if isinstance(holder, dict) and "linearize" in holder:
            raise SimError(
                f"{where}.linearize is not supported with analysis_steps[]: "
                "`linearize` is only available on a scalar `tran` analysis "
                '(analysis: {"kind": "tran", ..., "linearize": true})'
            )
        if is_declared(holder):
            raise SimError(
                f"{where}.post_commands is not supported with analysis_steps[]: "
                "`post_commands` is only available on a scalar `tran` analysis "
                '(analysis: {"kind": "tran", ..., "post_commands": [...]})'
            )


def validate_linearize(analysis: dict[str, Any]) -> None:
    """Issue #4014: ``analysis.linearize`` must be a boolean on a ``tran``."""
    if "linearize" not in analysis:
        return
    value = analysis["linearize"]
    if not isinstance(value, bool):
        raise SimError(
            f"analysis.linearize must be a boolean (got {value!r}); "
            "set true to resample the transient waveforms onto a uniform "
            "time grid before the expr measurements are evaluated"
        )
    if value and str(analysis.get("kind", "")).lower() != "tran":
        raise SimError(
            'analysis.linearize requires analysis.kind "tran" '
            f"(got {analysis.get('kind')!r}): `linearize` resamples a "
            "transient waveform and has no meaning for other analyses"
        )


def _validate_entry(index: int, entry: Any) -> str:
    where = f"analysis.post_commands[{index}]"
    if not isinstance(entry, str):
        raise SimError(f"{where} must be a string (got {entry!r})")
    if "\n" in entry or "\r" in entry or ";" in entry:
        raise SimError(
            f"{where} must be a single command line: newlines and ';' would "
            f"splice a second control command (got {entry!r})"
        )
    line = entry.strip(" \t")
    verb, *arguments = _SPACE.split(line, maxsplit=1)
    if verb not in ALLOWED_COMMANDS:
        raise SimError(
            f"{where} must start with one of {', '.join(ALLOWED_COMMANDS)} "
            f"(got {entry!r}): `post_commands` is a narrow allowlist, not a "
            "generic control-line passthrough"
        )
    if not _valid_arguments(verb, arguments[0] if arguments else ""):
        raise SimError(f"{where} has invalid waveform arguments for {verb!r}")
    return line


def validate(analysis: dict[str, Any], measurements_spec: list[Any]) -> None:
    """Refuse a malformed ``analysis.post_commands`` before any engine launch."""
    if "post_commands" not in analysis:
        return
    value = analysis["post_commands"]
    if not isinstance(value, list):
        raise SimError(
            f"analysis.post_commands must be a list of command strings (got {value!r})"
        )
    if not value:
        raise SimError(
            "analysis.post_commands must not be empty: omit the field instead"
        )
    if str(analysis.get("kind", "")).lower() != "tran":
        raise SimError(
            'analysis.post_commands requires analysis.kind "tran" '
            f"(got {analysis.get('kind')!r}): it transforms a transient waveform"
        )
    for index, entry in enumerate(value):
        _validate_entry(index, entry)
    for spec in measurements_spec:
        if (
            isinstance(spec, dict)
            and spec.get("expr") is None
            and spec.get("spice") is None
        ):
            raise SimError(
                f"measurement {spec.get('name')!r} cannot be combined with "
                "analysis.post_commands: only `expr` and `spice` measurements "
                "are supported, because other kinds read the transient plot "
                "after the post commands have replaced it"
            )


def control_lines(analysis: dict[str, Any]) -> list[str]:
    """The deck lines for ``analysis.post_commands`` (empty when undeclared)."""
    return [
        _validate_entry(index, entry)
        for index, entry in enumerate(analysis.get("post_commands") or [])
    ]


def refuse_xyce(engine: str, analysis: dict[str, Any]) -> None:
    """The Xyce deck has no ``.control`` block to carry these commands."""
    if engine == "xyce" and is_declared(analysis):
        raise SimError(
            "analysis.post_commands is not supported for engine 'xyce': they "
            "are ngspice `.control` commands, which the Xyce deck does not "
            "have; use engine 'ngspice' or drop the field"
        )
