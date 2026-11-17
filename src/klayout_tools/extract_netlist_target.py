"""Where ``klt extract`` writes its netlist (issue #5196's slice pattern,
pulled out for issue #5481).

Moved verbatim out of ``extract.py``: the default output path, the
``--no-netlist`` scratch-dir resolution and its flag rejections, and the
hash/cleanup step that closes a write. These four are the one piece of
``run_extract`` that is **engine-independent** -- ``klt extract --engine
klayout-runset`` (``extract_runset.py``) resolves and finishes its own write
through exactly the same helpers, so a layout extracted by either engine lands
in the same place, honours ``--no-netlist`` the same way and is hashed the
same way. They lived in ``extract.py``, which is at its file-size ceiling;
moving them is what let the second engine reuse them instead of copying.

``extract.py`` re-exports all four, so every existing
``from .extract import _resolve_netlist_target`` keeps working.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from ._provenance import sha256_file
from .extract_spef import ExtractError


def _default_output_path(path: str) -> str:
    """``<file>`` with its extension replaced by ``.spice`` (spike section 2a)."""
    stem, _ext = os.path.splitext(path)
    return f"{stem}.spice"


def _resolve_netlist_target(
    path: str, output: str | None, no_netlist: bool
) -> tuple[str, str | None]:
    """Where the netlist is written, plus the scratch dir to delete after (or
    ``None``). ``--no-netlist`` (issue #2850) writes into a private temp dir
    so nothing persists; otherwise ``output`` or the next-to-input default,
    with its parent directory created."""
    if no_netlist:
        scratch_dir = tempfile.mkdtemp(prefix="klt-extract-")
        return os.path.join(scratch_dir, "extracted.spice"), scratch_dir
    netlist_path = output if output is not None else _default_output_path(path)
    out_dir = os.path.dirname(os.path.abspath(netlist_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise ExtractError(f"cannot create output directory {out_dir}: {exc}") from exc
    return netlist_path, None


def _finish_netlist_write(
    netlist_path: str, scratch_dir: str | None
) -> tuple[str | None, str | None]:
    """``(reported netlist_path, netlist_sha256)``; both ``None`` after
    discarding the scratch dir of a ``--no-netlist`` run (issue #2850)."""
    if scratch_dir is not None:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return None, None
    return netlist_path, sha256_file(netlist_path)


def _validate_no_netlist_flags(
    no_netlist: bool,
    output: str | None,
    subcircuit_cell: str | None,
    subcircuit_output: str | None,
) -> None:
    """``--no-netlist`` (issue #2850) rejections: it contradicts ``-o`` and
    the ``--subcircuit`` family, which slices/hashes the written netlist."""
    if not no_netlist:
        return
    if output is not None:
        raise ExtractError(
            "--no-netlist cannot be combined with -o/--output -- no netlist "
            "is written, so there is nothing to place at that path"
        )
    if subcircuit_cell is not None or subcircuit_output is not None:
        raise ExtractError(
            "--no-netlist cannot be combined with --subcircuit/"
            "--subcircuit-output -- the sub-circuit deck is sliced from the "
            "written netlist"
        )
