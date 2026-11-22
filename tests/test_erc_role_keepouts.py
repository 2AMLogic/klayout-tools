"""ERC role keepouts grade conductor in the selected hierarchy, in top-cell DBU."""

import json
import re

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.coverage import work_id
from klayout_tools.erc import ErcError, run_erc, validate_erc_spec

_RULE = "erc.role_keepout"
_CORRIDOR = {"box": [1, 1, 3, 4], "roles": ["met1", "met2", "met3"], "below": "met3"}


def _spec(tmp_path, **extra):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "stackup": [
                    {"name": "poly", "layer": "1/0", "role": "gate"},
                    {"name": "local", "layer": "2/0"},
                    {"name": "met1", "layer": "3/0"},
                    {"name": "met2", "layer": "4/0"},
                    {"name": "met3", "layer": "5/0"},
                ],
                "vias": [{"name": "via1", "layer": "6/0", "between": ["met1", "met2"]}],
                **extra,
            }
        )
    )
    return str(path)


def _layout(tmp_path, offset=150, draw_met2=False):
    gds = tmp_path / "corridor.gds"
    layout = kdb.Layout()
    layout.dbu = 0.01
    top = layout.create_cell("TOP")
    child = layout.create_cell("WIRE")
    child.shapes(layout.layer(3, 0)).insert(kdb.Box(0, 0, 200, 50))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(offset, 170)))
    for layer in (2, 5):
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(100, 100, 300, 400))
    other = layout.create_cell("OTHER")
    other.shapes(layout.layer(4, 0)).insert(kdb.Box(100, 100, 300, 400))
    if draw_met2:
        top.shapes(layout.layer(4, 0)).insert(kdb.Box(-100, -100, -50, -50))
    layout.write(str(gds))
    return str(gds)


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        (None, ""),
        ({}, ".box"),
        ({**_CORRIDOR, "other": 0}, ".other"),
        ({**_CORRIDOR, "box": [0, 0, 1]}, ".box"),
        ({**_CORRIDOR, "box": [0, 0, True, 1]}, ".box"),
        ({**_CORRIDOR, "box": [0, 0, float("inf"), 1]}, ".box"),
        ({**_CORRIDOR, "box": [0, 0, 1, float("nan")]}, ".box"),
        ({**_CORRIDOR, "box": [1, 0, 1, 1]}, ".box"),
        ({**_CORRIDOR, "box": [0, 1, 1, -1]}, ".box"),
        ({**_CORRIDOR, "roles": None}, ".roles"),
        ({**_CORRIDOR, "roles": ["met1", "met1"]}, ".roles[1]"),
        ({**_CORRIDOR, "roles": ["via1"]}, ".roles[0]"),
        ({**_CORRIDOR, "roles": [None]}, ".roles[0]"),
        ({**_CORRIDOR, "below": "missing"}, ".below"),
    ],
)
def test_invalid_declaration_names_its_field(tmp_path, entry, field):
    path = _spec(tmp_path, keepouts=[_CORRIDOR, entry])
    with pytest.raises(ErcError, match=re.escape(f"keepouts[1]{field}")):
        validate_erc_spec(path)


def test_keepouts_must_be_array(tmp_path):
    with pytest.raises(ErcError, match="keepouts must be an array"):
        validate_erc_spec(_spec(tmp_path, keepouts=None))


@pytest.mark.parametrize("flags", [[], ["--findings-only"]])
def test_cli_clips_translated_unlabelled_wire(tmp_path, capsys, flags):
    path = _spec(tmp_path, keepouts=[_CORRIDOR, {**_CORRIDOR, "box": [4, 4, 5, 5]}])
    assert main(["erc", "--validate-spec", path, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["keepouts"] == 2
    assert (
        main(
            ["erc", _layout(tmp_path), path, "--top", "TOP", "--format", "json", *flags]
        )
        == 3
    )
    report = json.loads(capsys.readouterr().out)
    assert report["erc_status"] == "violations"
    assert report["erc_finding_count"] == 1
    (finding,) = report["erc_findings"]
    assert (
        finding["rule"],
        finding["keepout_index"],
        finding["role"],
        finding["layer"],
    ) == (_RULE, 0, "met1", "3/0")
    assert finding["bbox"] == {"left": 150, "bottom": 170, "right": 300, "top": 220}
    cov = report["erc_coverage"]
    assert cov["checked"] == [work_id(_RULE, i, "met1") for i in range(2)]
    assert cov["skipped"] == [
        {"id": work_id(_RULE, i, "met2"), "reason": "role_not_drawn"} for i in range(2)
    ]


@pytest.mark.parametrize("offset", [300, 301])
def test_edge_contact_and_outside_are_checked_clear(tmp_path, offset):
    report = run_erc(
        _layout(tmp_path, offset, draw_met2=True),
        _spec(tmp_path, keepouts=[_CORRIDOR]),
        top="TOP",
    )
    assert report["erc_findings"] == []
    assert report["erc_status"] == "clean"
    assert report["erc_coverage"]["checked"] == [
        work_id(_RULE, 0, r) for r in ("met1", "met2")
    ]


@pytest.mark.parametrize(
    ("entries", "ids", "reason"),
    [
        ([], [work_id(_RULE)], "empty_keepouts"),
        (
            [{**_CORRIDOR, "below": "met1"}],
            [work_id(_RULE, 0)],
            "no_roles_below_cutoff",
        ),
        ([{**_CORRIDOR, "roles": []}], [work_id(_RULE, 0)], "no_roles_below_cutoff"),
        (
            [{**_CORRIDOR, "box": [1, 1, 1.001, 2]}],
            [work_id(_RULE, 0, r) for r in ("met1", "met2")],
            "keepout_box_collapsed",
        ),
    ],
)
def test_empty_work_is_disclosed(tmp_path, entries, ids, reason):
    report = run_erc(_layout(tmp_path), _spec(tmp_path, keepouts=entries), top="TOP")
    assert report["erc_coverage"]["checked"] == []
    assert report["erc_coverage"]["skipped"] == [
        {"id": i, "reason": reason} for i in ids
    ]
    assert report["erc_status"] == "not_checked"


@pytest.mark.parametrize("draw_other", [True, False])
def test_no_declared_conductor_in_top_skips_keepouts(tmp_path, draw_other):
    gds = tmp_path / "empty.gds"
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    if draw_other:
        top.shapes(layout.layer(99, 0)).insert(kdb.Box(0, 0, 100, 100))
    layout.write(str(gds))
    report = run_erc(str(gds), _spec(tmp_path, keepouts=[_CORRIDOR]))
    assert report["erc_status"] == "not_checked"
    assert report["erc_coverage"]["skipped"] == [
        {"id": work_id(_RULE, 0, role), "reason": "role_not_drawn"}
        for role in ("met1", "met2")
    ]


@pytest.mark.parametrize(
    ("box", "status"),
    [
        ([-1e308, -1e308, 1e308, 1e308], "violations"),
        ([1e99, 0, 1e100, 4], "clean"),
    ],
)
def test_finite_boxes_beyond_stream_coordinate_range(tmp_path, box, status):
    report = run_erc(
        _layout(tmp_path, draw_met2=True),
        _spec(tmp_path, keepouts=[{**_CORRIDOR, "box": box}]),
        top="TOP",
    )
    assert report["erc_status"] == status
    assert len(report["erc_coverage"]["checked"]) == 2
