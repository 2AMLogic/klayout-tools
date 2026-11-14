"""xschem output capture and the opt-in ``klt netlist --erc`` gate (issue #4953).

Every local-backend payload carries a ``diagnostics`` block: xschem's
stdout/stderr, each capped to the last :data:`DIAGNOSTICS_TAIL_BYTES` bytes.
With ``erc=True`` the run additionally fails with
``error.code: "erc_findings"`` when xschem printed ``Error:`` lines (its
netlister's own ERC findings, e.g. ``Error: undriven node: #net1``), before
anything is written. Without ``erc`` behaviour and netlist bytes are unchanged.

Kept out of :mod:`klayout_tools.netlist` to hold that file at its size ceiling.
"""

from __future__ import annotations

import re
from typing import Any

#: ``error.code`` of the ``--erc`` gate.
ERC_FINDINGS_CODE = "erc_findings"

#: Per-stream cap (bytes of tail kept) for the ``diagnostics`` payload block.
DIAGNOSTICS_TAIL_BYTES = 4096

#: xschem prints its own netlister ERC findings (e.g. ``Error: undriven node:
#: #net1``) on stdout/stderr during a normal batch run; measured on 3.4.7 --
#: it has no ``-erc`` flag (``Unknown option``), so the signal is read here.
#: Measured on 3.4.7: a dangling wire (``undriven node``) and shorted pins
#: (``Error: Net shorted: A - B``) are both caught (issue #4962).
_ERC_LINE_RE = re.compile(r"^\s*Error:", re.MULTILINE)


def _tail(text: str, limit: int = DIAGNOSTICS_TAIL_BYTES) -> dict[str, Any]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return {"text": text, "truncated": False}
    kept = raw[-limit:].decode("utf-8", errors="replace")
    return {"text": kept, "truncated": True}


def _erc_findings(stdout: str, stderr: str) -> list[str]:
    found: list[str] = []
    for stream in (stdout, stderr):
        for line in stream.splitlines():
            if _ERC_LINE_RE.match(line):
                found.append(line.strip())
    return found


def check_erc(stdout: str, stderr: str) -> None:
    """Raise ``NetlistError(erc_findings)`` when xschem reported ERC findings."""
    from .netlist import NetlistError  # lazy: netlist imports this module

    findings = _erc_findings(stdout, stderr)
    if not findings:
        return
    shown = findings[:20]
    more = len(findings) - len(shown)
    raise NetlistError(
        f"xschem reported {len(findings)} ERC finding(s): "
        + "; ".join(shown)
        + (f"; ... and {more} more" if more else "")
        + ". Nothing was written.",
        code=ERC_FINDINGS_CODE,
        details={"findings": shown, "finding_count": len(findings)},
    )


def diagnostics_block(stdout: str, stderr: str, *, erc: bool = False) -> dict[str, Any]:
    """The payload's ``diagnostics`` block; with ``erc`` it first gates on ERC."""
    if erc:
        check_erc(stdout, stderr)
    out, err = _tail(stdout), _tail(stderr)
    return {
        "stdout": out["text"],
        "stderr": err["text"],
        "stdout_truncated": out["truncated"],
        "stderr_truncated": err["truncated"],
    }
