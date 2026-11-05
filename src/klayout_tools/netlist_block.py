"""``klt netlist --block`` post-processing (issues #2680, #3899, #4490).

Split out of :mod:`klayout_tools.netlist` to keep that file under its
size-ratchet ceiling; these are pure bytes-in/bytes-out transforms.
"""

from __future__ import annotations

import re
from typing import Any

#: xschem comments out the top sheet's own ``.subckt``/``.ends`` pair when it
#: netlists it as a testbench (``**.subckt ...`` / ``**.ends``). Matches the
#: first such line so it can be uncommented; the capture group is everything
#: after the ``**`` (preserving the directive's own arguments verbatim).
_BLOCK_SUBCKT_COMMENT_RE = re.compile(
    r"^([ \t]*)\*\*([ \t]*\.subckt\b.*)$", re.IGNORECASE | re.MULTILINE
)
#: xschem wraps a long top-sheet header onto commented continuation lines
#: (``*+ ...``); once the header is uncommented they must become real SPICE
#: ``+`` continuations or the port list is silently truncated (issue #3899).
_BLOCK_CONTINUATION_COMMENT_RE = re.compile(r"^([ \t]*)\*{1,2}\+(.*)$")
_BLOCK_ENDS_COMMENT_RE = re.compile(
    r"^([ \t]*)\*\*([ \t]*\.ends\b.*)$", re.IGNORECASE | re.MULTILINE
)

#: xschem always appends a trailing ``.end`` -- correct for a complete deck,
#: wrong for an includable block (a second ``.end`` in the including
#: testbench's own deck would terminate it early). Matches only when ``.end``
#: is the last line of the file.
_TRAILING_END_RE = re.compile(r"\n[ \t]*\.end[ \t]*\n?\Z", re.IGNORECASE)


def _rejoin_header_continuations(text: str) -> str:
    """Rewrite the commented ``*+``/``**+`` lines contiguous with the
    just-uncommented ``.subckt`` header into real ``+`` continuations."""
    lines = text.split("\n")
    header = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"[ \t]*\.subckt\b", line, re.IGNORECASE)
        ),
        None,
    )
    if header is None:
        return text
    j = header + 1
    while j < len(lines):
        match = _BLOCK_CONTINUATION_COMMENT_RE.match(lines[j])
        if not match:
            break
        lines[j] = f"{match.group(1)}+{match.group(2)}"
        j += 1
    return "\n".join(lines)


_SUBCKT_HEADER_RE = re.compile(r"[ \t]*\.subckt\b", re.IGNORECASE)
_PARAMS_RE = re.compile(r"\bparams:", re.IGNORECASE)


def space_separate_subckt_ports(data: bytes) -> bytes:
    """Rewrite comma-joined bus ports in ``.subckt`` headers to spaces
    (issue #4490).

    xschem emits a bus port as ``a[1],a[0]``; ngspice mis-maps the ports of a
    subcircuit with two or more such groups (trailing nodes silently left
    unconnected). Only the port part of ``.subckt`` header lines and their
    ``+`` continuations changes -- text from ``params:`` onward, comments and
    device cards are left alone. A bus-free netlist is returned unchanged.
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    in_header = False
    in_params = False
    for i, line in enumerate(lines):
        if _SUBCKT_HEADER_RE.match(line):
            in_header, in_params = True, False
        elif not (in_header and line.lstrip().startswith("+")):
            in_header = False
            continue
        if in_params:
            continue
        match = _PARAMS_RE.search(line)
        head, tail = (
            (line[: match.start()], line[match.start() :]) if match else (line, "")
        )
        if match:
            in_params = True
        lines[i] = re.sub(r"[ \t]*,[ \t]*", " ", head) + tail
    return "\n".join(lines).encode("utf-8")


def apply_block_mode(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Post-process a testbench-shaped xschem export into an includable block
    (issue #2680, edge 1).

    xschem comments out the top sheet's own ``.subckt``/``.ends`` pair and
    always appends a trailing ``.end`` -- correct when the top sheet is a
    testbench, wrong when it is a reusable block meant to be ``.include``d
    elsewhere. This uncomments the first commented ``.subckt``/``.ends`` pair
    found and drops the trailing ``.end``, so the result can be ``.include``d
    by a separate testbench deck without a duplicate ``.end`` terminating it
    early.

    Never raises: a schematic that was not shaped like a testbench export
    (no commented header, no trailing ``.end``) is passed through unchanged,
    with the gap reported in the returned info dict's ``warning`` rather than
    failing the whole run -- the netlist itself is still valid output.
    """
    text = data.decode("utf-8", errors="replace")

    text, n_subckt = _BLOCK_SUBCKT_COMMENT_RE.subn(r"\1\2", text, count=1)
    if n_subckt:
        text = _rejoin_header_continuations(text)
    text, n_ends = _BLOCK_ENDS_COMMENT_RE.subn(r"\1\2", text, count=1)
    text, n_end = _TRAILING_END_RE.subn("\n", text, count=1)

    subckt_uncommented = n_subckt > 0
    ends_uncommented = n_ends > 0
    trailing_end_removed = n_end > 0

    warning = None
    missing = []
    if not subckt_uncommented:
        missing.append("no commented '**.subckt' header found to uncomment")
    if not ends_uncommented:
        missing.append("no commented '**.ends' found to uncomment")
    if not trailing_end_removed:
        missing.append("no trailing '.end' line found to remove")
    if missing:
        warning = (
            "--block requested but " + "; ".join(missing) + " -- the export "
            "may not have been shaped like a testbench netlist; it was "
            "written as xschem produced it"
        )

    info = {
        "requested": True,
        "subckt_uncommented": subckt_uncommented,
        "ends_uncommented": ends_uncommented,
        "trailing_end_removed": trailing_end_removed,
        "warning": warning,
    }
    return text.encode("utf-8"), info
