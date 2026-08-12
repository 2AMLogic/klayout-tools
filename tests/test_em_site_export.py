"""Tests for the geode-fem site export format (issue #849, Phase 1a of Epic
#840) -- `docs/schemas/em-site-export.schema.json` and the committed patch-
antenna instance it validates, `examples/em/patch_antenna/patch_antenna.em-export.json`.

Mirrors `tests/test_socket_check.py`'s schema-self-consistency +
instance-validates pattern for `docs/schemas/socket.schema.json`. This file
does not (and cannot, in CI) re-run geode-fem itself -- see
`examples/em/patch_antenna/generate.py` for the reproducible generation
script and `docs/design/em-site-export-format.md` for the full design
rationale. What's tested here is that the schema is well-formed, the
committed export validates against it, and the export's internal
consistency invariants (frame/mesh vertex-count alignment, cell indices in
bounds, non-fabricated provenance) hold -- the executable form of the
claims made in that design doc.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "schemas"
    / "em-site-export.schema.json"
)
EXPORT_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "em"
    / "patch_antenna"
    / "patch_antenna.em-export.json"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _load_export() -> dict:
    return json.loads(EXPORT_PATH.read_text())


# ---------------------------------------------------------------------------
# Schema self-consistency
# ---------------------------------------------------------------------------


def test_schema_file_is_valid_json():
    schema = json.loads(SCHEMA_PATH.read_text())
    assert isinstance(schema, dict)


def test_schema_is_well_formed_json_schema():
    schema = _load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)


# ---------------------------------------------------------------------------
# The committed patch-antenna export validates against the schema
# ---------------------------------------------------------------------------


def test_patch_antenna_export_exists():
    assert EXPORT_PATH.is_file(), f"missing committed export: {EXPORT_PATH}"


def test_patch_antenna_export_validates_against_schema():
    jsonschema.validate(instance=_load_export(), schema=_load_schema())


def test_minimal_instance_validates():
    """A minimal, non-generated instance also validates -- confirms the
    schema doesn't accidentally require patch-antenna-specific fields (e.g.
    `radiation_pattern`) that a different future benchmark export wouldn't
    have."""
    minimal = {
        "schema_version": 1,
        "benchmark": "toy",
        "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "cells": [[0, 1, 2]]},
        "frames": [{"label": "f0"}],
        "s_parameters": {
            "ports": ["p1"],
            "points": [],
            "resonance": {"f_res_hz": 1.0e9, "s11_dip_db": -10.0},
        },
        "provenance": {
            "generator": {"repo": "https://example.invalid/x", "commit": "0" * 40},
            "geometry": {"fixture": "x.msh"},
            "generated_at": "2026-01-01T00:00:00Z",
        },
    }
    jsonschema.validate(instance=minimal, schema=_load_schema())


def test_instance_missing_provenance_fails_schema():
    broken = {
        "schema_version": 1,
        "benchmark": "toy",
        "mesh": {"vertices": [], "cells": []},
        "frames": [],
        "s_parameters": {
            "ports": [],
            "points": [],
            "resonance": {"f_res_hz": 1.0, "s11_dip_db": -10.0},
        },
    }
    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=broken, schema=_load_schema())


# ---------------------------------------------------------------------------
# Internal consistency of the committed export (beyond what JSON Schema
# alone can express -- cross-field invariants).
# ---------------------------------------------------------------------------


def test_frames_are_vertex_aligned_with_mesh():
    export = _load_export()
    n = len(export["mesh"]["vertices"])
    assert n > 0
    for frame in export["frames"]:
        if "scalar" in frame:
            assert len(frame["scalar"]) == n
        if "vector" in frame:
            assert len(frame["vector"]) == n


def test_cell_indices_are_in_bounds():
    export = _load_export()
    n = len(export["mesh"]["vertices"])
    for cell in export["mesh"]["cells"]:
        assert len(cell) >= 3
        for idx in cell:
            assert 0 <= idx < n


def test_cells_are_triangles_or_larger_polygons_not_degenerate():
    export = _load_export()
    vertices = export["mesh"]["vertices"]
    for cell in export["mesh"]["cells"]:
        # No repeated vertex index within one cell (degenerate polygon).
        assert len(set(cell)) == len(cell)
        # All cell vertices genuinely lie on the documented slice plane.
        zs = {vertices[i][2] for i in cell}
        assert len(zs) == 1


def test_provenance_traces_to_a_real_commit():
    """`generator.commit` must look like a real 40-hex git sha, never a
    placeholder -- the "no illustrative/faked data" requirement made
    checkable."""
    export = _load_export()
    commit = export["provenance"]["generator"]["commit"]
    assert len(commit) == 40
    int(commit, 16)  # raises ValueError if not hex
    assert commit != "0" * 40


def test_provenance_fixture_hash_is_present_and_hex():
    export = _load_export()
    fixture_sha256 = export["provenance"]["geometry"]["fixture_sha256"]
    assert fixture_sha256 is not None
    assert len(fixture_sha256) == 64
    int(fixture_sha256, 16)


def test_s_parameters_points_cover_a_real_sweep():
    export = _load_export()
    points = export["s_parameters"]["points"]
    assert len(points) >= 2
    freqs = [p["frequency_hz"] for p in points]
    assert freqs == sorted(freqs)
    assert len(set(freqs)) == len(freqs)


def test_bandwidth_null_carries_an_explanatory_note():
    """The schema allows `bandwidth_10db_hz: null`, but only paired with a
    reason -- a silent `null` would be indistinguishable from "we forgot to
    fill this in"."""
    export = _load_export()
    resonance = export["s_parameters"]["resonance"]
    if resonance.get("bandwidth_10db_hz") is None:
        assert resonance.get("bandwidth_10db_note")


def test_radiation_pattern_cuts_are_equal_length():
    export = _load_export()
    pattern = export.get("radiation_pattern")
    if not pattern:
        return
    for cut in pattern.get("cuts", {}).values():
        assert len(cut["theta_deg"]) == len(cut["e_norm"])
        assert len(cut["theta_deg"]) > 0


def test_mesh_reduction_documents_the_slice_and_crop():
    export = _load_export()
    reduction = export["provenance"].get("mesh_reduction")
    assert reduction is not None
    assert "slice_z_mm" in reduction
    assert "crop_bbox_mm" in reduction
