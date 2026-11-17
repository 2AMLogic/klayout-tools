"""``analysis.post_commands`` for ``klt sim`` (issue #6080).

A waveform-transform control command (``linearize``, ``fft``) has to run
*between* the transient and the ``measurements[].expr`` lets: after ``fft`` the
current plot is the spectrum, so the ``expr`` entries read spectrum bins.
``analysis.post_commands`` declares that sequence on a scalar ``tran``::

    {"analysis": {"kind": "tran", "args": "1u 2m",
                  "post_commands": ["linearize v(a) v(b)", "fft v(a) v(b)"]}}

The lines are emitted in order after the analysis line, after the rawfile
``write`` and the ``linearize`` flag, and before the ``expr`` lets. Each entry
is one line whose first token is on the narrow allowlist
:data:`ALLOWED_COMMANDS` -- this is *not* a generic control-line passthrough.

Control lines other than ``expr``/``spice`` measurements (crossings, energy,
eye, ...) assume the transient plot is current, which stops being true after
``fft``; such measurement kinds are refused together with ``post_commands``.

``analysis`` is already part of the resume fingerprint and is forwarded to
off-host workers verbatim, so a request without the field is byte-identical to
before; a runner that predates the field is gated in
``sim_batch_contract_spec._RUNNER_REQUEST_FEATURES``.
"""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError

#: First tokens a ``post_commands`` entry may start with.
ALLOWED_COMMANDS = ("linearize", "fft")


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


def _validate_entry(index: int, entry: Any) -> None:
    where = f"analysis.post_commands[{index}]"
    if not isinstance(entry, str):
        raise SimError(f"{where} must be a string (got {entry!r})")
    if "\n" in entry or "\r" in entry or ";" in entry:
        raise SimError(
            f"{where} must be a single command line: newlines and ';' would "
            f"splice a second control command (got {entry!r})"
        )
    tokens = entry.split()
    if not tokens or tokens[0] not in ALLOWED_COMMANDS:
        raise SimError(
            f"{where} must start with one of {', '.join(ALLOWED_COMMANDS)} "
            f"(got {entry!r}): `post_commands` is a narrow allowlist, not a "
            "generic control-line passthrough"
        )


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
    return [entry.strip() for entry in analysis.get("post_commands") or []]


def refuse_xyce(engine: str, analysis: dict[str, Any]) -> None:
    """The Xyce deck has no ``.control`` block to carry these commands."""
    if engine == "xyce" and is_declared(analysis):
        raise SimError(
            "analysis.post_commands is not supported for engine 'xyce': they "
            "are ngspice `.control` commands, which the Xyce deck does not "
            "have; use engine 'ngspice' or drop the field"
        )
