"""Per-block metrics extractor — emit a normalized ``layout.json`` per block.

This module aggregates output that already exists (or that ``klt`` already
knows how to compute) into a single, stable ``layout.json`` data contract
consumed by the klayout-tools.org gallery (epic #13, issue #61 — mirrors
kicad-tools' ``kct board-metrics``, rjwalters/kicad-tools#3676).

It does **not** recompute metrics ad hoc: layer/cell/instance counts are
produced by calling this repo's own :func:`~klayout_tools.layers.layers_report`
and :func:`~klayout_tools.cells.cells_report`, and DRC violation counts (when
a ``--deck`` is supplied) by :func:`~klayout_tools.drc.run_drc` — the exact
same library functions that back ``klt layers`` / ``klt cells`` / ``klt drc``.

A "block" is a directory containing:

* One GDSII/OASIS layout file at the top level. Preferred name is
  ``layout.gds`` or ``layout.oas``; otherwise the single ``*.gds``,
  ``*.gds.gz``, or ``*.oas`` file found there is used. A block with no such
  file is reported with ``status: "no_artifacts"`` rather than raising.
* An optional ``meta.json`` with ``name`` and/or ``description`` string
  fields, used verbatim when present. ``name`` falls back to a title-cased
  version of the block directory name (the slug) when absent.
* An optional ``output/renders/*.png`` directory (written by ``klt render``,
  #60) — every PNG found there is listed in ``renders``, keyed by filename
  stem, sorted for determinism.

Output is written to ``<block_dir>/output/layout.json``.

layout.json schema (v1)
------------------------

All fields except ``schema_version``, ``generated_at``, ``slug``, ``name``,
and ``status`` are OPTIONAL — they are omitted (never ``null``) when the
source data is absent or unavailable.

::

    {
      "schema_version": 1,
      "generated_at": "<ISO-8601 UTC timestamp>",
      "slug": "example-block",
      "name": "Example Block",
      "description": "A short description of the block.",
      "layout_file": "layout.gds",
      "layer_count": 12,
      "cell_count": 34,
      "instance_count": 120,
      "drc": {"deck": "sky130", "status": "clean", "violation_count": 0},
      "renders": {"metal1": "renders/metal1.png"},
      "signals": {
        "schema_version": 1,
        "engine": "ngspice",
        "status": "pass",
        "corner_count": 12,
        "passed": 12,
        "failed": 0,
        "errored": 0,
        "measurements": [],
        "corners": []
      },
      "status": "ok"
    }

``status`` is one of:

* ``"ok"``           — a layout file was found and ``klt layers``/``klt
                        cells`` both parsed it successfully.
* ``"partial"``      — a layout file was found but could not be fully
                        parsed (unreadable/corrupt stream).
* ``"no_artifacts"`` — no layout file was found under the block directory.

``drc`` is populated only when a ``--deck`` is supplied *and* the DRC run
succeeds; it never affects ``status`` (DRC is opt-in per invocation, not a
required artifact). ``renders`` is populated only when at least one PNG is
found under ``output/renders/``. ``signals`` is populated only when
``output/sim/signals.json`` exists (written by whatever build step ran
``klt sim`` for this block -- see :func:`_attach_signals`) and is attached
verbatim -- it is a ``klt sim``-response-shaped dict (``docs/cli/sim.md``),
not a shape this module defines or validates itself. Additive, like
``renders``/``drc``: introducing it required no ``schema_version`` bump.

Schema versioning policy: this schema is the data contract the Astro gallery
site's loader (#59) and detail/index pages (#63/#64) consume. Field additions
must be additive — no renames, no type changes. Bump ``schema_version`` only
for breaking changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cells import CellsError, cells_report
from .drc import DrcError, run_drc
from .layers import LayersError, layers_report

__all__ = [
    "LayoutMetricsError",
    "layout_metrics_report",
    "emit_layout_json",
]

SCHEMA_VERSION = 1

#: Preferred explicit layout filenames, checked before falling back to a
#: glob over extensions.
_PREFERRED_NAMES = ("layout.gds", "layout.oas", "layout.gds.gz")

#: Extensions considered a layout stream when scanning a block directory's
#: top level. Order matters only for the glob pattern, not for preference.
_LAYOUT_GLOBS = ("*.gds", "*.gds.gz", "*.oas")


class LayoutMetricsError(Exception):
    """Raised when the block directory itself cannot be processed.

    This is a usage-level error (bad path), distinct from a block merely
    lacking artifacts — a missing layout file, DRC deck, or renders
    directory is handled gracefully via the ``status``/omitted-field
    contract, not by raising.
    """


def _find_layout_file(block_dir: Path) -> Path | None:
    """Locate the block's layout file, or ``None`` if none is present.

    Prefers an explicitly named ``layout.gds``/``layout.oas``/``layout.gds.gz``.
    Otherwise, if exactly one file matching a known layout extension exists at
    the top level, uses it. If multiple candidates exist with no preferred
    name, the alphabetically first is used (deterministic, not a guess about
    intent) — ambiguity here is a smell in the block layout itself, not
    something this function can resolve further.
    """
    for name in _PREFERRED_NAMES:
        candidate = block_dir / name
        if candidate.is_file():
            return candidate

    candidates: list[Path] = []
    for pattern in _LAYOUT_GLOBS:
        candidates.extend(p for p in block_dir.glob(pattern) if p.is_file())

    if not candidates:
        return None
    return sorted(candidates)[0]


def _default_name(slug: str) -> str:
    """Title-cased fallback name derived from the slug (e.g. ``a-b`` -> ``A B``)."""
    return slug.replace("-", " ").replace("_", " ").title()


def _load_meta(block_dir: Path) -> dict[str, str]:
    """Read optional ``name``/``description`` overrides from ``meta.json``.

    Missing file, unreadable file, or malformed JSON are all treated as "no
    metadata" rather than raised — metadata is a courtesy, not a required
    artifact.
    """
    meta_path = block_dir / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    name = data.get("name")
    if isinstance(name, str) and name:
        out["name"] = name
    description = data.get("description")
    if isinstance(description, str) and description:
        out["description"] = description
    return out


def _attach_renders(metrics: dict[str, Any], output_dir: Path) -> None:
    """Attach a ``renders`` dict for every PNG found under ``output/renders/``.

    Keys are filename stems (e.g. ``metal1.png`` -> ``metal1``); values are
    paths relative to ``layout.json``'s own location (``output/``), so they
    resolve as ``output/<value>``. Omitted entirely when no renders exist.
    """
    renders_dir = output_dir / "renders"
    if not renders_dir.is_dir():
        return
    renders: dict[str, str] = {}
    for png in sorted(renders_dir.glob("*.png")):
        renders[png.stem] = f"renders/{png.name}"
    if renders:
        metrics["renders"] = renders


def _attach_signals(metrics: dict[str, Any], output_dir: Path) -> None:
    """Attach ``signals`` when ``output/sim/signals.json`` exists.

    That file is written by whatever build-time step ran ``klt sim`` for
    this block (for the gallery's 7 standard cells: ``scripts/
    gallery_signals.py``, invoked from ``scripts/bootstrap-gallery-blocks.
    py`` -- see issue #99) — its content is attached **verbatim** as
    ``signals`` (already a ``klt sim``-shaped dict, see ``docs/cli/sim.md``
    and ``docs/cli/layout-metrics.md``'s ``signals`` field docs). Missing
    file, unreadable file, or malformed JSON are all treated as "no
    signals" rather than raised, same as ``_load_meta``.
    """
    signals_path = output_dir / "sim" / "signals.json"
    if not signals_path.is_file():
        return
    try:
        data = json.loads(signals_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        metrics["signals"] = data


def layout_metrics_report(block_dir: str, deck: str | None = None) -> dict[str, Any]:
    """Read existing block artifacts and return a ``layout.json`` dict.

    Never raises on missing/partial per-block artifacts (layout file, DRC
    deck, renders) — those are reflected in the ``status`` field and by
    omitting the corresponding keys. Only raises :class:`LayoutMetricsError`
    when ``block_dir`` itself is not a usable directory.

    Args:
        block_dir: Path to a block directory (e.g. ``blocks/example-block``).
        deck: Optional DRC deck name (e.g. ``"sky130"``) to run via ``klt
            drc``. When omitted, the ``drc`` field is not included.

    Returns:
        A schema-conforming ``layout.json`` dict (see module docstring).
    """
    path = Path(block_dir)
    if not path.exists():
        raise LayoutMetricsError(f"directory not found: {block_dir}")
    if not path.is_dir():
        raise LayoutMetricsError(f"not a directory: {block_dir}")

    slug = path.name
    meta = _load_meta(path)

    metrics: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "name": meta.get("name", _default_name(slug)),
    }
    if "description" in meta:
        metrics["description"] = meta["description"]

    output_dir = path / "output"
    layout_file = _find_layout_file(path)

    if layout_file is None:
        metrics["status"] = "no_artifacts"
        _attach_renders(metrics, output_dir)
        _attach_signals(metrics, output_dir)
        return metrics

    metrics["layout_file"] = layout_file.relative_to(path).as_posix()

    parsed_ok = True
    try:
        layers = layers_report(str(layout_file))
        metrics["layer_count"] = layers["layer_count"]
    except LayersError:
        parsed_ok = False

    try:
        cells = cells_report(str(layout_file))
        metrics["cell_count"] = cells["cell_count"]
        metrics["instance_count"] = sum(entry["instances"] for entry in cells["cells"])
    except CellsError:
        parsed_ok = False

    if deck:
        try:
            drc = run_drc(str(layout_file), deck)
            metrics["drc"] = {
                "deck": deck,
                "status": drc["status"],
                "violation_count": drc["violation_count"],
            }
        except DrcError:
            # DRC is opt-in and orthogonal to layer/cell parsing -- a failed
            # or unsupported deck omits `drc` without affecting `status`.
            pass

    _attach_renders(metrics, output_dir)
    _attach_signals(metrics, output_dir)

    metrics["status"] = "ok" if parsed_ok else "partial"
    return metrics


def emit_layout_json(
    block_dir: str, deck: str | None = None, output_path: str | None = None
) -> Path:
    """Extract metrics and write ``layout.json``; return the written path.

    Args:
        block_dir: Block directory (e.g. ``blocks/example-block``).
        deck: Optional DRC deck name, forwarded to :func:`layout_metrics_report`.
        output_path: Override output path. Defaults to
            ``<block_dir>/output/layout.json``.

    Returns:
        The path the ``layout.json`` was written to.
    """
    metrics = layout_metrics_report(block_dir, deck=deck)

    resolved_output = (
        Path(output_path)
        if output_path is not None
        else Path(block_dir) / "output" / "layout.json"
    )
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(json.dumps(metrics, indent=2) + "\n")
    return resolved_output
