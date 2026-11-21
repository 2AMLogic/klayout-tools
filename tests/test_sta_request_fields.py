"""Unknown-request-key rejection and SPEF name-mismatch hint for ``klt sta``
(issue #5065). Split from ``test_post_route_sta.py`` (file-size ratchet)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools._sta_request_diag import (
    KNOWN_REQUEST_FIELDS,
    SPEF_NAME_MISMATCH_HINT,
)
from klayout_tools.post_route_sta import PostRouteStaError, run_sta

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _request(tmp_path: Path, **overrides) -> str:
    (tmp_path / "top.def").write_text("# def\n", encoding="utf-8")
    request = {
        "def": "top.def",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
    }
    request.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_run_rejects_unknown_top_level_key(tmp_path):
    request_path = _request(tmp_path, netlist="top.v")
    with pytest.raises(PostRouteStaError, match="unknown field.*netlist.*known fields"):
        run_sta(request_path)


def test_run_accepts_schema_key(tmp_path):
    request_path = _request(
        tmp_path, schema="klt.sta.request/1", pdk={"corner": "tt_025C_1v80"}
    )
    with pytest.raises(PostRouteStaError, match="pdk.cell_library is required"):
        run_sta(request_path)


def test_known_fields_cover_documented_keys():
    assert {"def", "verilog", "spef", "sdf", "macros"} <= set(KNOWN_REQUEST_FIELDS)


def test_spef_name_mismatch_hint_names_extract_flags():
    assert "--def-net-names" in SPEF_NAME_MISMATCH_HINT
    assert "--def-pins" in SPEF_NAME_MISMATCH_HINT
