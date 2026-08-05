"""Tests for `klt socket-check` and the `run_socket_check` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus, mirroring `tests/test_drc.py` and
`tests/test_precheck.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.socket_check import CHECK_NAMES, SocketCheckError, run_socket_check

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "schemas" / "socket.schema.json"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _write_layout(layout: kdb.Layout, tmp_path: Path, name: str = "layout.gds") -> str:
    path = tmp_path / name
    layout.write(str(path))
    return str(path)


def _write_socket(descriptor: dict, tmp_path: Path, name: str = "socket.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(descriptor))
    return str(path)


def _basic_descriptor(**overrides) -> dict:
    descriptor = {
        "schema": "klt.socket/1",
        "name": "example_block",
        "outline": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "pins": [
            {
                "name": "VDD",
                "layer": [67, 20],
                "x": 1.0,
                "y": 1.0,
                "tolerance_um": 0.0,
            }
        ],
        "reserved_layers": [[64, 20]],
        "budgets": [
            {
                "name": "signal_r_max",
                "value": 500,
                "unit": "ohm",
                "notes": "worst-case corner",
            }
        ],
    }
    descriptor.update(overrides)
    return descriptor


def _layout_with_pin(
    *,
    text: str = "VDD",
    layer: tuple[int, int] = (67, 20),
    x_um: float = 1.0,
    y_um: float = 1.0,
    dbu_um: float = 0.001,
) -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = dbu_um
    top = layout.create_cell("TOP")
    label_layer = layout.layer(layer[0], layer[1])
    x_dbu = round(x_um / dbu_um)
    y_dbu = round(y_um / dbu_um)
    top.shapes(label_layer).insert(kdb.Text(text, kdb.Trans(kdb.Vector(x_dbu, y_dbu))))
    return layout


# ---------------------------------------------------------------------------
# schema self-consistency
# ---------------------------------------------------------------------------


def test_schema_file_is_valid_json():
    schema = json.loads(SCHEMA_PATH.read_text())
    assert isinstance(schema, dict)


def test_schema_is_well_formed_json_schema():
    schema = _load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)


def test_basic_descriptor_validates_against_schema():
    jsonschema.validate(instance=_basic_descriptor(), schema=_load_schema())


def test_minimal_descriptor_validates_against_schema():
    minimal = {
        "schema": "klt.socket/1",
        "outline": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
    }
    jsonschema.validate(instance=minimal, schema=_load_schema())


def test_descriptor_missing_outline_fails_schema():
    broken = {"schema": "klt.socket/1"}
    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=broken, schema=_load_schema())


def test_descriptor_with_unknown_top_level_field_fails_schema():
    broken = _basic_descriptor()
    broken["unexpected_field"] = "nope"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=broken, schema=_load_schema())


# ---------------------------------------------------------------------------
# overall report shape / determinism
# ---------------------------------------------------------------------------


def test_run_socket_check_all_checks_present_and_pass_on_matching_layout(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)

    assert report["schema_version"] == 1
    assert report["file"] == layout_path
    assert report["socket"] == socket_path
    assert report["socket_name"] == "example_block"
    assert report["dbu_um"] == 0.001
    assert report["status"] == "pass"
    assert report["check_count"] == len(CHECK_NAMES)
    assert [c["name"] for c in report["checks"]] == list(CHECK_NAMES)
    for check in report["checks"]:
        assert check["status"] in {"pass", "skipped"}
        assert check["violation_count"] == 0


def test_run_socket_check_deterministic_across_runs(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    assert run_socket_check(layout_path, socket_path) == run_socket_check(
        layout_path, socket_path
    )


def test_run_socket_check_raises_on_missing_layout(tmp_path):
    socket_path = _write_socket(_basic_descriptor(), tmp_path)
    with pytest.raises(SocketCheckError):
        run_socket_check("/no/such/path/design.gds", socket_path)


def test_run_socket_check_raises_on_missing_descriptor(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    with pytest.raises(SocketCheckError):
        run_socket_check(layout_path, "/no/such/path/socket.json")


def test_run_socket_check_raises_on_malformed_descriptor_json(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = tmp_path / "socket.json"
    socket_path.write_text("not json")
    with pytest.raises(SocketCheckError):
        run_socket_check(layout_path, str(socket_path))


def test_run_socket_check_raises_on_descriptor_missing_outline(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket({"schema": "klt.socket/1"}, tmp_path)
    with pytest.raises(SocketCheckError):
        run_socket_check(layout_path, socket_path)


def test_run_socket_check_raises_on_descriptor_bad_outline_ordering(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    descriptor = _basic_descriptor(
        outline={"x0": 10.0, "y0": 0.0, "x1": 0.0, "y1": 10.0}
    )
    socket_path = _write_socket(descriptor, tmp_path)
    with pytest.raises(SocketCheckError):
        run_socket_check(layout_path, socket_path)


# ---------------------------------------------------------------------------
# pins
# ---------------------------------------------------------------------------


def test_pins_skipped_when_descriptor_declares_no_pins(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(
        _basic_descriptor(pins=[], reserved_layers=[]), tmp_path
    )

    report = run_socket_check(layout_path, socket_path)
    (pins,) = [c for c in report["checks"] if c["name"] == "pins"]

    assert pins["status"] == "skipped"
    assert pins["skip_reason"] == "socket descriptor declares no pins"


def test_pins_pass_when_label_matches_layer_and_position(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (pins,) = [c for c in report["checks"] if c["name"] == "pins"]

    assert pins["status"] == "pass"


def test_pins_flags_missing_pin(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")  # no labels at all
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (pins,) = [c for c in report["checks"] if c["name"] == "pins"]

    assert pins["status"] == "fail"
    assert pins["violation_count"] == 1
    violation = pins["violations"][0]
    assert violation["pin"] == "VDD"
    assert violation["reason"] == "missing"
    assert violation["actual"] == []


def test_pins_flags_misplaced_pin(tmp_path):
    # Label present, right layer, but 2um away from the declared position --
    # exceeds the descriptor's tolerance_um of 0.0.
    layout_path = _write_layout(_layout_with_pin(x_um=3.0, y_um=3.0), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (pins,) = [c for c in report["checks"] if c["name"] == "pins"]

    assert pins["status"] == "fail"
    assert pins["violation_count"] == 1
    violation = pins["violations"][0]
    assert violation["pin"] == "VDD"
    assert violation["reason"] == "misplaced"
    assert violation["expected"]["x_um"] == 1.0
    assert violation["actual"][0]["x_um"] == 3.0


def test_pins_within_tolerance_passes(tmp_path):
    layout_path = _write_layout(_layout_with_pin(x_um=1.0005, y_um=1.0), tmp_path)
    descriptor = _basic_descriptor(reserved_layers=[])
    descriptor["pins"][0]["tolerance_um"] = 0.01
    socket_path = _write_socket(descriptor, tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (pins,) = [c for c in report["checks"] if c["name"] == "pins"]

    assert pins["status"] == "pass"


def test_pins_flags_wrong_layer(tmp_path):
    layout_path = _write_layout(
        _layout_with_pin(layer=(68, 5), x_um=1.0, y_um=1.0), tmp_path
    )
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (pins,) = [c for c in report["checks"] if c["name"] == "pins"]

    assert pins["status"] == "fail"
    violation = pins["violations"][0]
    assert violation["pin"] == "VDD"
    assert violation["reason"] == "wrong_layer"
    assert violation["expected"]["layer"] == "67/20"
    assert violation["actual"][0]["layer"] == "68/5"


# ---------------------------------------------------------------------------
# outline
# ---------------------------------------------------------------------------


def test_outline_passes_when_all_geometry_inside(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met1 = layout.layer(68, 20)
    top.shapes(met1).insert(kdb.Box(0, 0, 5000, 5000))  # within 0-10um box
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(
        _basic_descriptor(pins=[], reserved_layers=[]), tmp_path
    )

    report = run_socket_check(layout_path, socket_path)
    (outline,) = [c for c in report["checks"] if c["name"] == "outline"]

    assert outline["status"] == "pass"


def test_outline_flags_geometry_outside_outline(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met1 = layout.layer(68, 20)
    layout.set_info(met1, kdb.LayerInfo(68, 20, "met1.drawing"))
    # 15um right edge -- outside the 0-10um outline.
    top.shapes(met1).insert(kdb.Box(0, 0, 15000, 500))
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(
        _basic_descriptor(pins=[], reserved_layers=[]), tmp_path
    )

    report = run_socket_check(layout_path, socket_path)
    (outline,) = [c for c in report["checks"] if c["name"] == "outline"]

    assert outline["status"] == "fail"
    assert outline["violation_count"] == 1
    violation = outline["violations"][0]
    assert violation["cell"] == "TOP"
    assert violation["layer"] == "68/20"
    assert violation["bbox_um"] == {"x0": 0.0, "y0": 0.0, "x1": 15.0, "y1": 0.5}


def test_outline_ignores_text_labels(tmp_path):
    """A pin label sitting exactly on the outline boundary/beyond it is a
    `pins` concern (misplaced), not an `outline` violation -- text carries
    no polygon."""
    layout = _layout_with_pin(x_um=1.0, y_um=1.0)
    top = layout.cell("TOP")
    stray_label_layer = layout.layer(99, 5)
    top.shapes(stray_label_layer).insert(
        kdb.Text("STRAY", kdb.Trans(kdb.Vector(50000, 50000)))
    )
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(
        _basic_descriptor(pins=[], reserved_layers=[]), tmp_path
    )

    report = run_socket_check(layout_path, socket_path)
    (outline,) = [c for c in report["checks"] if c["name"] == "outline"]

    assert outline["status"] == "pass"


# ---------------------------------------------------------------------------
# reserved_layers
# ---------------------------------------------------------------------------


def test_reserved_layers_skipped_when_descriptor_declares_none(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (reserved,) = [c for c in report["checks"] if c["name"] == "reserved_layers"]

    assert reserved["status"] == "skipped"
    assert reserved["skip_reason"] == ("socket descriptor declares no reserved layers")


def test_reserved_layers_flags_usage(tmp_path):
    layout = _layout_with_pin()
    top = layout.cell("TOP")
    reserved = layout.layer(64, 20)
    layout.set_info(reserved, kdb.LayerInfo(64, 20, "nwell.drawing"))
    top.shapes(reserved).insert(kdb.Box(0, 0, 100, 100))
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (reserved_check,) = [c for c in report["checks"] if c["name"] == "reserved_layers"]

    assert reserved_check["status"] == "fail"
    assert reserved_check["violation_count"] == 1
    violation = reserved_check["violations"][0]
    assert violation["layer"] == "64/20"
    # GDSII itself carries no layer-name property (unlike a .lyp file) --
    # `layout.set_info(...)`'s name does not survive a write/re-read round
    # trip, so `name` is `None` here, matching `layer_whitelist`'s own
    # documented `info.name if info.name else None` fallback.
    assert violation["name"] is None
    assert violation["shapes"] == 1


def test_reserved_layers_passes_when_layer_absent(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    report = run_socket_check(layout_path, socket_path)
    (reserved,) = [c for c in report["checks"] if c["name"] == "reserved_layers"]

    assert reserved["status"] == "pass"


# ---------------------------------------------------------------------------
# --top (issue #554)
# ---------------------------------------------------------------------------


def _make_multi_top_layout() -> kdb.Layout:
    """Two independent top cells on the reserved layer (64, 20): only
    ``BAD_TOP`` draws on it, so `--top` scoping is directly observable in
    the `reserved_layers` check -- the whole-stream ``Layout.each_cell()``
    walk `_check_reserved_layers` uses regardless of the per-cell-scoped
    `pins`/`outline` checks."""
    layout = kdb.Layout()
    reserved = layout.layer(64, 20)
    layout.set_info(reserved, kdb.LayerInfo(64, 20, "nwell.drawing"))

    good = layout.create_cell("GOOD_TOP")
    good.shapes(reserved)  # declared, no shapes

    bad = layout.create_cell("BAD_TOP")
    bad.shapes(reserved).insert(kdb.Box(0, 0, 100, 100))

    return layout


def test_top_scopes_reserved_layers_check(tmp_path):
    layout_path = _write_layout(_make_multi_top_layout(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    report_good = run_socket_check(layout_path, socket_path, top="GOOD_TOP")
    (reserved_good,) = [
        c for c in report_good["checks"] if c["name"] == "reserved_layers"
    ]
    assert reserved_good["status"] == "pass"

    report_bad = run_socket_check(layout_path, socket_path, top="BAD_TOP")
    (reserved_bad,) = [
        c for c in report_bad["checks"] if c["name"] == "reserved_layers"
    ]
    assert reserved_bad["status"] == "fail"
    assert reserved_bad["violations"][0]["shapes"] == 1

    # Default (no --top) behaviour is unchanged: every top cell is walked.
    report_all = run_socket_check(layout_path, socket_path)
    (reserved_all,) = [
        c for c in report_all["checks"] if c["name"] == "reserved_layers"
    ]
    assert reserved_all["status"] == "fail"


def test_top_unknown_cell_raises(tmp_path):
    layout_path = _write_layout(_make_multi_top_layout(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    with pytest.raises(SocketCheckError, match="top cell not found in stream: NOPE"):
        run_socket_check(layout_path, socket_path, top="NOPE")


def test_cli_top_flag_scopes_report(tmp_path, capsys):
    # `GOOD_TOP` carries no shapes and no `VDD` label, so the `pins` check
    # still fails regardless of `--top` scoping -- assert the scoped field
    # (`reserved_layers`) directly via JSON rather than the overall exit
    # code, which the unrelated `pins` check also affects.
    layout_path = _write_layout(_make_multi_top_layout(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    main(
        [
            "socket-check",
            layout_path,
            "--socket",
            socket_path,
            "--top",
            "GOOD_TOP",
            "--format",
            "json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    (reserved,) = [c for c in data["checks"] if c["name"] == "reserved_layers"]
    assert reserved["status"] == "pass"


def test_cli_unknown_top_exits_one(tmp_path, capsys):
    layout_path = _write_layout(_make_multi_top_layout(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path)

    assert (
        main(["socket-check", layout_path, "--socket", socket_path, "--top", "NOPE"])
        == 1
    )
    err = capsys.readouterr().err
    assert "top cell not found in stream: NOPE" in err


# ---------------------------------------------------------------------------
# budgets
# ---------------------------------------------------------------------------


def test_budgets_reported_declared_unverified_and_never_fail_status(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)

    assert report["status"] == "pass"
    assert report["budgets"] == [
        {
            "name": "signal_r_max",
            "value": 500.0,
            "unit": "ohm",
            "notes": "worst-case corner",
            "status": "declared_unverified",
        }
    ]


def test_budgets_empty_when_descriptor_declares_none(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(
        _basic_descriptor(budgets=[], reserved_layers=[]), tmp_path
    )

    report = run_socket_check(layout_path, socket_path)

    assert report["budgets"] == []


# ---------------------------------------------------------------------------
# overall status / CLI wiring
# ---------------------------------------------------------------------------


def test_overall_status_fails_if_any_check_fails(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")  # no labels -> missing pin
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)

    assert report["status"] == "fail"


def test_overall_status_ignores_skipped_checks(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    report = run_socket_check(layout_path, socket_path)

    assert report["status"] == "pass"
    assert any(c["status"] == "skipped" for c in report["checks"])


def test_json_contract(tmp_path, capsys):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    assert (
        main(
            [
                "socket-check",
                str(layout_path),
                "--socket",
                str(socket_path),
                "--format",
                "json",
            ]
        )
        == 3
    )
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "socket",
        "socket_name",
        "dbu_um",
        "status",
        "check_count",
        "checks",
        "budgets",
    }
    assert data["schema_version"] == 1
    assert data["status"] == "fail"
    assert data["check_count"] == len(CHECK_NAMES)

    for check in data["checks"]:
        assert set(check.keys()) == {
            "name",
            "status",
            "violation_count",
            "violations",
            "skip_reason",
        }
        assert check["status"] in {"pass", "fail", "skipped"}


def test_exit_code_pass(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)
    assert (
        main(
            [
                "socket-check",
                str(layout_path),
                "--socket",
                str(socket_path),
                "--format",
                "json",
            ]
        )
        == 0
    )


def test_exit_code_fail(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)
    assert (
        main(
            [
                "socket-check",
                str(layout_path),
                "--socket",
                str(socket_path),
                "--format",
                "json",
            ]
        )
        == 3
    )


def test_default_format_is_text(tmp_path, capsys):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(_basic_descriptor(reserved_layers=[]), tmp_path)

    assert main(["socket-check", str(layout_path), "--socket", str(socket_path)]) == 3
    out = capsys.readouterr().out
    assert "status: fail" in out
    assert "pins: fail" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_missing_socket_descriptor_is_application_error(tmp_path, capsys):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)

    assert (
        main(
            [
                "socket-check",
                str(layout_path),
                "--socket",
                "/no/such/socket.json",
                "--format",
                "json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "socket-check"


def test_cli_missing_socket_flag_is_usage_error(tmp_path):
    layout_path = _write_layout(_layout_with_pin(), tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["socket-check", str(layout_path)])
    assert excinfo.value.code == 2
