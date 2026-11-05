"""Generation-input record for ``klt netlist`` (issue #4945).

Split out of :mod:`klayout_tools.netlist` to keep that file under its
size-ratchet ceiling. The record is a ``**`` comment line prepended to a
written netlist so ``--check`` can tell a tool-environment change from a
schematic change.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

#: Comment line :func:`run_netlist` prepends to a written netlist (issue #4945)
#: recording the tool inputs that generated it, so ``--check`` can tell a
#: tool-environment change from a schematic change. A ``**`` comment, so it is
#: inert to SPICE and ignorable with ``--ignore-comments``.
INPUTS_RECORD_PREFIX = b"** klt-netlist-inputs: "
_INPUTS_RECORD_RE = re.compile(rb"^\*\* klt-netlist-inputs: (.*?)\r?\n", re.MULTILINE)


def generation_inputs(
    version: str | None, rcfile: str | None, pdk_block: dict[str, Any] | None
) -> dict[str, str | None]:
    """The host-independent inputs that shape xschem's output: the xschem
    version, the xschemrc *basename* (its directory is checkout-specific) and
    the PDK variant."""
    return {
        "xschem_version": version,
        "rcfile": os.path.basename(rcfile) if rcfile else None,
        "pdk_variant": pdk_block["variant"] if pdk_block is not None else None,
    }


def inputs_record_line(inputs: dict[str, str | None]) -> bytes:
    return INPUTS_RECORD_PREFIX + json.dumps(inputs, sort_keys=True).encode() + b"\n"


def split_inputs_record(data: bytes) -> tuple[bytes, dict[str, Any] | None]:
    """``data`` without its inputs-record line, plus the parsed record
    (``None`` for a legacy netlist, or one whose record is unparseable)."""
    match = _INPUTS_RECORD_RE.search(data)
    if match is None:
        return data, None
    stripped = data[: match.start()] + data[match.end() :]
    try:
        record = json.loads(match.group(1))
    except ValueError:
        return stripped, None
    return stripped, record if isinstance(record, dict) else None


def inputs_drift(
    committed: dict[str, Any] | None, fresh: dict[str, str | None]
) -> tuple[dict[str, Any], str]:
    """The ``drift.inputs`` block and the sentence appended to ``reason``."""
    if committed is None:
        return (
            {"committed": None, "fresh": fresh, "changed": None},
            "the committed netlist records no generation inputs (written "
            "before klt recorded them), so a tool-environment change cannot "
            "be told from a schematic change -- re-run without --check to "
            "write the record",
        )
    changed = sorted(k for k in fresh if committed.get(k) != fresh[k])
    block = {"committed": committed, "fresh": fresh, "changed": changed}
    if not changed:
        return block, (
            "the recorded generation inputs are unchanged, so the schematic "
            "(or a symbol it uses) changed"
        )
    detail = ", ".join(f"{k}: {committed.get(k)} -> {fresh[k]}" for k in changed)
    return block, (
        f"the generation inputs changed since the committed netlist was "
        f"written ({detail}), which may explain the drift"
    )
