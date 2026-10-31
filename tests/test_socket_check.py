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
        # Issue #2839: additive top-level fact block.
        "power_ports",
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


# ---------------------------------------------------------------------------
# power_ports (issue #2839): edge-accessible supply ports
# ---------------------------------------------------------------------------

MET1 = (68, 20)
MET1_LABEL = (68, 5)
MET4 = (71, 20)


def _supply_layout(
    *,
    vdd_box: tuple[float, float, float, float] = (0.0, 4.0, 1.0, 5.0),
    vss_box: tuple[float, float, float, float] = (9.0, 4.0, 10.0, 5.0),
    vdd_layer: tuple[int, int] = MET1,
    extra_met1: list[tuple[float, float, float, float]] = (),
    label_layer: tuple[int, int] | None = None,
) -> kdb.Layout:
    """A 10x10 um block with a VDD and a VSS port, each a drawn box with its
    label at the box centre. Labels sit on the conductor layer itself unless
    ``label_layer`` is given (the sky130 68/5-label-over-68/20 convention)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    def box(x0, y0, x1, y1):
        return kdb.Box(
            round(x0 * 1000), round(y0 * 1000), round(x1 * 1000), round(y1 * 1000)
        )

    for name, rect, layer in (("VDD", vdd_box, vdd_layer), ("VSS", vss_box, MET1)):
        top.shapes(layout.layer(*layer)).insert(box(*rect))
        cx = round((rect[0] + rect[2]) / 2 * 1000)
        cy = round((rect[1] + rect[3]) / 2 * 1000)
        text_layer = layout.layer(*(label_layer or layer))
        top.shapes(text_layer).insert(kdb.Text(name, kdb.Trans(kdb.Vector(cx, cy))))
    for rect in extra_met1:
        top.shapes(layout.layer(*MET1)).insert(box(*rect))
    return layout


def _supply_descriptor(
    *,
    vdd_box: tuple[float, float, float, float] = (0.0, 4.0, 1.0, 5.0),
    vss_box: tuple[float, float, float, float] = (9.0, 4.0, 10.0, 5.0),
    vdd_layer: tuple[int, int] = MET1,
    label_layer: tuple[int, int] | None = None,
    power_ports: dict | None = None,
    pins: list | None = None,
) -> dict:
    def pin(name, rect, layer, use):
        entry = {
            "name": name,
            "layer": list(label_layer or layer),
            "x": (rect[0] + rect[2]) / 2,
            "y": (rect[1] + rect[3]) / 2,
            "use": use,
        }
        if label_layer is not None:
            entry["metal_layer"] = list(layer)
        return entry

    descriptor = _basic_descriptor(
        pins=pins
        if pins is not None
        else [
            pin("VDD", vdd_box, vdd_layer, "POWER"),
            pin("VSS", vss_box, MET1, "GROUND"),
        ],
        reserved_layers=[],
        budgets=[],
    )
    if power_ports is not None:
        descriptor["power_ports"] = power_ports
    return descriptor


def _power_ports_check(report: dict) -> dict:
    return next(c for c in report["checks"] if c["name"] == "power_ports")


def _run_supply(tmp_path, *, layout_kwargs=None, descriptor_kwargs=None, **kwargs):
    layout_path = _write_layout(_supply_layout(**(layout_kwargs or {})), tmp_path)
    socket_path = _write_socket(
        _supply_descriptor(**(descriptor_kwargs or {})), tmp_path
    )
    return run_socket_check(layout_path, socket_path, **kwargs)


def test_power_ports_check_is_skipped_unless_requested(tmp_path):
    """No descriptor `power_ports` object and no flag: the check is skipped
    (no existing descriptor changes verdict), but the facts are still
    reported for a consumer to re-grade."""
    report = _run_supply(
        tmp_path,
        layout_kwargs={"vdd_box": (4.0, 4.0, 5.0, 5.0)},
        descriptor_kwargs={"vdd_box": (4.0, 4.0, 5.0, 5.0)},
    )

    assert report["status"] == "pass"
    assert _power_ports_check(report)["status"] == "skipped"
    block = report["power_ports"]
    assert block["requested"] is False
    assert block["parameters"] == {
        "edge_band_um": 0.0,
        "edges": ["left", "bottom", "right", "top"],
        "allowed_layers": None,
        "provisional": True,
    }
    assert block["outline_um"] == {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0}
    vdd = next(p for p in block["ports"] if p["name"] == "VDD")
    assert vdd["geometry_source"] == "drawn"
    assert vdd["edge_distance_um"] == {
        "left": 4.0,
        "bottom": 4.0,
        "right": 5.0,
        "top": 5.0,
    }


def test_power_ports_pass_when_supplies_touch_the_outline(tmp_path):
    report = _run_supply(tmp_path, descriptor_kwargs={"power_ports": {}})

    check = _power_ports_check(report)
    assert check["status"] == "pass", check
    assert report["status"] == "pass"
    vdd = next(p for p in report["power_ports"]["ports"] if p["name"] == "VDD")
    assert vdd["edge_distance_um"]["left"] == 0.0
    assert vdd["channel_clear"]["left"] is True
    assert vdd["use_source"] == "declared"


def test_power_ports_fail_interior_only(tmp_path):
    interior = (4.0, 4.0, 5.0, 5.0)
    report = _run_supply(
        tmp_path,
        layout_kwargs={"vdd_box": interior},
        descriptor_kwargs={"vdd_box": interior, "power_ports": {}},
    )

    check = _power_ports_check(report)
    assert check["status"] == "fail"
    assert check["violations"] == [
        {"pin": "VDD", "reason": "interior_only", "use": "POWER"}
    ]
    assert report["status"] == "fail"


def test_power_ports_band_boundary_is_inclusive(tmp_path):
    """A port exactly `edge_band_um` from the edge counts; any narrower band
    does not."""
    inset = (0.5, 4.0, 1.5, 5.0)  # 0.5 um from the left edge
    kwargs = {"layout_kwargs": {"vdd_box": inset}}

    at_band = _run_supply(
        tmp_path,
        descriptor_kwargs={"vdd_box": inset, "power_ports": {"edge_band_um": 0.5}},
        **kwargs,
    )
    assert _power_ports_check(at_band)["status"] == "pass"

    narrower = _run_supply(
        tmp_path,
        descriptor_kwargs={"vdd_box": inset, "power_ports": {"edge_band_um": 0.5}},
        edge_band_um=0.499,
        **kwargs,
    )
    assert _power_ports_check(narrower)["violations"][0]["reason"] == "interior_only"
    assert narrower["power_ports"]["parameters"]["edge_band_um"] == 0.499


def test_power_ports_channel_blocked_by_other_conductor(tmp_path):
    """Within the band, but another same-layer shape sits between the port
    and the edge: an integrator cannot land a wire on it from outside."""
    inset = (0.5, 4.0, 1.5, 5.0)
    report = _run_supply(
        tmp_path,
        layout_kwargs={"vdd_box": inset, "extra_met1": [(0.1, 4.2, 0.3, 4.8)]},
        descriptor_kwargs={"vdd_box": inset, "power_ports": {"edge_band_um": 1.0}},
    )

    assert _power_ports_check(report)["violations"] == [
        {"pin": "VDD", "reason": "channel_blocked", "use": "POWER"}
    ]


def test_power_ports_disallowed_layer(tmp_path):
    report = _run_supply(
        tmp_path,
        descriptor_kwargs={"power_ports": {"allowed_layers": [list(MET4)]}},
    )

    violations = _power_ports_check(report)["violations"]
    assert {v["pin"]: v["reason"] for v in violations} == {
        "VDD": "layer_disallowed",
        "VSS": "layer_disallowed",
    }


def test_power_ports_missing_ground_pin(tmp_path):
    descriptor = _supply_descriptor(power_ports={})
    descriptor["pins"] = descriptor["pins"][:1]
    layout_path = _write_layout(_supply_layout(), tmp_path)
    socket_path = _write_socket(descriptor, tmp_path)

    report = run_socket_check(layout_path, socket_path)

    assert _power_ports_check(report)["violations"] == [
        {"pin": None, "reason": "no_ground_pin", "use": "GROUND"}
    ]


def test_power_ports_label_without_conductor_is_not_a_port(tmp_path):
    """A label with no drawn metal under it is nothing to connect to."""
    layout = _supply_layout(label_layer=MET1_LABEL)
    layout.top_cell().shapes(layout.layer(*MET1)).clear()
    layout_path = _write_layout(layout, tmp_path)
    socket_path = _write_socket(
        _supply_descriptor(label_layer=MET1_LABEL, power_ports={}), tmp_path
    )

    report = run_socket_check(layout_path, socket_path)

    reasons = {v["pin"]: v["reason"] for v in _power_ports_check(report)["violations"]}
    assert reasons == {"VDD": "no_drawn_geometry", "VSS": "no_drawn_geometry"}


def test_power_ports_metal_layer_separate_from_label_layer(tmp_path):
    """sky130 labels met1 on 68/5 over conductor on 68/20: `metal_layer`
    names the conductor so the port geometry is found."""
    report = _run_supply(
        tmp_path,
        layout_kwargs={"label_layer": MET1_LABEL},
        descriptor_kwargs={"label_layer": MET1_LABEL, "power_ports": {}},
    )

    assert _power_ports_check(report)["status"] == "pass"
    vdd = next(p for p in report["power_ports"]["ports"] if p["name"] == "VDD")
    assert vdd["layer"] == "68/5"
    assert vdd["metal_layer"] == "68/20"


def test_power_ports_use_falls_back_to_lef_abstract_heuristic(tmp_path):
    descriptor = _supply_descriptor(power_ports={})
    for pin in descriptor["pins"]:
        pin.pop("use")
    layout_path = _write_layout(_supply_layout(), tmp_path)
    socket_path = _write_socket(descriptor, tmp_path)

    report = run_socket_check(layout_path, socket_path)

    uses = {
        p["name"]: (p["use"], p["use_source"]) for p in report["power_ports"]["ports"]
    }
    assert uses == {"VDD": ("POWER", "heuristic"), "VSS": ("GROUND", "heuristic")}
    assert _power_ports_check(report)["status"] == "pass"


def test_power_ports_descriptor_validates_against_schema():
    descriptor = _supply_descriptor(
        label_layer=MET1_LABEL,
        power_ports={
            "edge_band_um": 0.5,
            "edges": ["left"],
            "allowed_layers": [[68, 20]],
        },
    )
    jsonschema.validate(instance=descriptor, schema=_load_schema())


@pytest.mark.parametrize(
    "power_ports",
    [
        "yes",
        {"edge_band_um": -1},
        {"edges": ["north"]},
        {"edges": []},
        {"allowed_layers": ["met1"]},
        {"unknown": 1},
    ],
)
def test_power_ports_malformed_descriptor_is_rejected(tmp_path, power_ports):
    layout_path = _write_layout(_supply_layout(), tmp_path)
    socket_path = _write_socket(_supply_descriptor(power_ports=power_ports), tmp_path)

    with pytest.raises(SocketCheckError, match="power_ports"):
        run_socket_check(layout_path, socket_path)


def test_cli_require_power_ports_flag(tmp_path, capsys):
    interior = (4.0, 4.0, 5.0, 5.0)
    layout_path = _write_layout(_supply_layout(vdd_box=interior), tmp_path)
    socket_path = _write_socket(_supply_descriptor(vdd_box=interior), tmp_path)

    base = ["socket-check", layout_path, "--socket", socket_path, "--format", "json"]
    assert main(base) == 0
    capsys.readouterr()
    assert main([*base, "--require-power-ports"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["power_ports"]["requested"] is True
    assert main([*base, "--require-power-ports", "--edge-band-um", "4"]) == 0
