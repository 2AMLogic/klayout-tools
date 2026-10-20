"""`klt drc --engine both` (issue #3973): offline tests of the curated-vs-native
cross-check. Both engines are replaced by fakes returning report-shaped dicts,
so these exercise the comparison logic only (no klayout binary, no PDK)."""

from __future__ import annotations

import json

import pytest

import klayout_tools.drc_crosscheck as cc
from klayout_tools.cli import main
from klayout_tools.coverage import build_check_coverage, coverage_rollup, rollup_status
from klayout_tools.drc import CODE_KLAYOUT_UNAVAILABLE, DrcError

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HASH = "sha256:" + "a" * 64
MAPPING = [
    {"id": "m1.width", "curated_rules": ["met1.1"], "native_categories": ["m1.1"]}
]


def _box(left=0, bottom=0, right=10, top=10):
    return {"left": left, "bottom": bottom, "right": right, "top": top}


def _violation(rule, bbox=None):
    return {"rule": rule, "bbox": bbox or _box()}


def _report(
    *,
    engine,
    violations=(),
    checked=(),
    categories=(),
    layout_hash=HASH,
    pdk="sky130A",
    known=True,
):
    """A report with the fields the cross-check reads. ``known=False`` models
    a native run whose coverage is unmeasured (`coverage_unknown`, exit 4)."""
    violations = list(violations)
    unknown = [] if known else [{"id": "engine_execution:klayout", "reason": "x"}]
    coverage = {
        **build_check_coverage(checked=list(checked), unknown=unknown),
        "rules_checked": sorted(checked),
        "rule_categories": sorted(categories),
    }
    report = {"coverage": coverage}
    rollup = coverage_rollup(report, failed=bool(violations))
    status = rollup_status(rollup, success="clean", failure="violations")
    return {
        "schema_version": 2,
        "file": "x.gds",
        "deck": "d",
        **({"engine": "klayout"} if engine == "native" else {}),
        "status": status,
        "violation_count": len(violations),
        "violations": violations,
        "coverage": coverage,
        "provenance": {
            "klt_version": "1",
            "klayout_version": "0.30",
            "pdk": {"name": pdk, "source": "s", "version": "v"},
            "deck": {"name": engine, "content_hash": HASH},
            "input": {"content_hash": layout_hash, "role": "layout"},
        },
    }


def _run(monkeypatch, curated, native, mapping=MAPPING):
    monkeypatch.setattr(cc, "run_drc", lambda *a, **k: curated)
    if isinstance(native, Exception):

        def boom(*a, **k):
            raise native

        monkeypatch.setattr(cc, "run_drc_klayout_engine", boom)
    else:
        monkeypatch.setattr(cc, "run_drc_klayout_engine", lambda *a, **k: native)
    return cc.run_drc_crosscheck("x.gds", "sky130", "native.drc", rule_mapping=mapping)


def _covered(**kw):
    return dict(checked=["met1.1"]), dict(categories=["m1.1"], checked=["m1.1"], **kw)


def test_agree_clean_and_raw_reports_are_embedded_verbatim(monkeypatch):
    c_kw, n_kw = _covered()
    curated = _report(engine="curated", **c_kw)
    native = _report(engine="native", **n_kw)
    out = _run(monkeypatch, curated, native)
    assert out["comparison"]["state"] == "agree_clean"
    assert out["reports"]["curated"] is curated
    assert out["reports"]["native"] is native
    assert out["status"] == "clean"
    assert cc.crosscheck_exit_code(out) == 0


def test_agree_violations_ignores_marker_count_granularity(monkeypatch):
    c_kw, n_kw = _covered()
    curated = _report(engine="curated", violations=[_violation("met1.1")], **c_kw)
    native = _report(
        engine="native",
        violations=[
            _violation("m1.1", _box(0, 0, 6, 10)),
            _violation("m1.1", _box(5, 0, 10, 10)),
        ],
        **n_kw,
    )
    out = _run(monkeypatch, curated, native)
    assert out["comparison"]["state"] == "agree_violations"
    assert out["status"] == "violations"
    assert cc.crosscheck_exit_code(out) == 3


@pytest.mark.parametrize("flagged_by", ["curated", "native"])
def test_one_sided_finding_is_a_disagreement(monkeypatch, flagged_by):
    c_kw, n_kw = _covered()
    c_v = [_violation("met1.1")] if flagged_by == "curated" else []
    n_v = [_violation("m1.1")] if flagged_by == "native" else []
    out = _run(
        monkeypatch,
        _report(engine="curated", violations=c_v, **c_kw),
        _report(engine="native", violations=n_v, **n_kw),
    )
    assert out["comparison"]["state"] == "disagreement"
    group = out["comparison"]["groups"][0]
    assert group["state"] == "disagreement"
    assert group["only_" + flagged_by] == [_box()]


def test_disjoint_locations_disagree_even_when_both_nonzero(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch,
        _report(
            engine="curated",
            violations=[_violation("met1.1", _box(0, 0, 5, 5))],
            **c_kw,
        ),
        _report(
            engine="native",
            violations=[_violation("m1.1", _box(50, 50, 60, 60))],
            **n_kw,
        ),
    )
    assert out["comparison"]["state"] == "disagreement"


def test_unmapped_rules_and_narrower_native_coverage_are_never_agreement(monkeypatch):
    # Both clean, but the curated deck ran a rule no mapping group pairs.
    curated = _report(engine="curated", checked=["met1.1", "met2.1"])
    native = _report(engine="native", categories=["m1.1"], checked=["m1.1"])
    out = _run(monkeypatch, curated, native)
    assert out["comparison"]["state"] == "not_comparable"
    assert out["comparison"]["unmapped"]["curated"] == ["met2.1"]


def test_partial_coverage_group_is_not_covered(monkeypatch):
    # The native deck never declared the mapped category: no comparison.
    curated = _report(engine="curated", checked=["met1.1"])
    native = _report(engine="native", categories=[], checked=[])
    out = _run(monkeypatch, curated, native)
    assert out["comparison"]["groups"][0]["state"] == "not_covered"
    assert out["comparison"]["state"] == "not_comparable"


def test_empty_mapping_is_not_comparable_never_agree(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch,
        _report(engine="curated", **c_kw),
        _report(engine="native", **n_kw),
        mapping=[],
    )
    assert out["comparison"]["state"] == "not_comparable"


@pytest.mark.parametrize(
    "exc, state",
    [
        (DrcError("no klayout", code=CODE_KLAYOUT_UNAVAILABLE), "native_unavailable"),
        (DrcError("deck exploded"), "engine_failed"),
    ],
)
def test_native_failure_is_not_checked_never_clean(monkeypatch, exc, state):
    curated = _report(engine="curated", checked=["met1.1"])
    out = _run(monkeypatch, curated, exc)
    assert out["comparison"]["state"] == state
    assert out["reports"]["native"] is None
    assert out["reports"]["curated"] is curated
    assert out["status"] == "not_checked" or out["status"] == curated["status"]
    assert out["status"] != "clean"
    assert cc.crosscheck_exit_code(out) == 4


def test_missing_native_deck_is_native_unavailable(monkeypatch):
    monkeypatch.setattr(
        cc, "run_drc", lambda *a, **k: _report(engine="curated", checked=["a"])
    )
    out = cc.run_drc_crosscheck("x.gds", "sky130", None)
    assert out["comparison"]["state"] == "native_unavailable"
    assert cc.crosscheck_exit_code(out) == 4


def test_native_failure_does_not_mask_curated_violations(monkeypatch):
    curated = _report(
        engine="curated", violations=[_violation("met1.1")], checked=["met1.1"]
    )
    out = _run(monkeypatch, curated, DrcError("x", code=CODE_KLAYOUT_UNAVAILABLE))
    assert out["status"] == "violations"
    assert cc.crosscheck_exit_code(out) == 3


def test_mismatched_layout_identity_is_an_error(monkeypatch):
    c_kw, n_kw = _covered()
    with pytest.raises(DrcError, match="different layouts"):
        _run(
            monkeypatch,
            _report(engine="curated", **c_kw),
            _report(engine="native", layout_hash="sha256:" + "b" * 64, **n_kw),
        )


def test_mismatched_pdk_identity_is_an_error(monkeypatch):
    c_kw, n_kw = _covered()
    with pytest.raises(DrcError, match="different PDK"):
        _run(
            monkeypatch,
            _report(engine="curated", **c_kw),
            _report(engine="native", pdk="gf180mcuD", **n_kw),
        )


def test_native_without_verdict_makes_clean_curated_run_no_verdict(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch,
        _report(engine="curated", **c_kw),
        _report(engine="native", known=False, **n_kw),
    )
    assert out["status"] == "coverage_unknown"
    assert cc.crosscheck_exit_code(out) == 4


def test_identity_block_records_shared_inputs(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch, _report(engine="curated", **c_kw), _report(engine="native", **n_kw)
    )
    ident = out["identity"]
    assert ident["layout_content_hash"] == HASH
    assert ident["pdk"]["name"] == "sky130A"
    assert ident["curated_deck"]["content_hash"] == HASH
    assert ident["native_deck"]["content_hash"] == HASH


def test_cli_engine_both_rejects_rules(tmp_path, capsys):
    gds = tmp_path / "a.gds"
    gds.write_bytes(b"")
    rc = main(
        [
            "drc",
            str(gds),
            "--engine",
            "both",
            "--deck",
            "sky130",
            "--rules",
            "x",
            "--format",
            "json",
        ]
    )
    assert rc == 1
    assert "--rules is not supported with --engine both" in capsys.readouterr().err


def test_cli_engine_both_emits_envelope_and_exit_code(monkeypatch, tmp_path, capsys):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch, _report(engine="curated", **c_kw), _report(engine="native", **n_kw)
    )
    monkeypatch.setattr(
        "klayout_tools.cli.drc_cmd.run_drc_crosscheck", lambda *a, **k: out
    )
    monkeypatch.setattr(
        "klayout_tools.cli.drc_cmd.pdk_module.drc_deck_file", lambda **k: "native.drc"
    )
    rc = main(
        ["drc", "x.gds", "--engine", "both", "--deck", "sky130", "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["mode"] == "crosscheck"
    assert payload["comparison"]["state"] == "agree_clean"


# --------------------------------------------------------------------------- #
# Schema conformance against real engine reports
# --------------------------------------------------------------------------- #

_STUB_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category><name>W.1</name><description>min width</description></category>
 </categories>
 <cells><cell><name>TOP</name></cell></cells>
 <items/>
</report-database>
"""


def _schema_validator():
    from pathlib import Path

    import jsonschema
    from referencing import Registry, Resource

    base = Path(__file__).resolve().parent.parent / "docs" / "schemas"
    schema = json.loads((base / "drc-response.schema.json").read_text())
    coverage = json.loads((base / "coverage.schema.json").read_text())
    registry = Registry().with_resource(
        coverage["$id"], Resource.from_contents(coverage)
    )
    return jsonschema.Draft202012Validator(schema, registry=registry)


def _tiny_gds(tmp_path):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(68, 20)).insert(kdb.Box(0, 0, 1000, 1000))
    path = tmp_path / "t.gds"
    layout.write(str(path))
    return str(path)


def test_crosscheck_envelope_validates_with_native_unavailable(tmp_path):
    out = cc.run_drc_crosscheck(_tiny_gds(tmp_path), "sky130", None)
    assert out["comparison"]["state"] == "native_unavailable"
    _schema_validator().validate(out)


def test_crosscheck_envelope_validates_with_real_reports(tmp_path, monkeypatch):
    import subprocess

    import klayout_tools.drc_klayout_engine as engine_module
    from helpers.subprocess_fakes import fake_completed

    real_run = subprocess.run

    def fake_run(cmd, *args, **kw):
        reports = [a for a in cmd if isinstance(a, str) and a.startswith("report=")]
        if not reports:
            return real_run(cmd, *args, **kw)
        report = reports[0].split("=", 1)[1]
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(_STUB_RDB)
        return fake_completed()

    monkeypatch.setattr(engine_module.subprocess, "run", fake_run)
    deck = tmp_path / "deck.lydrc"
    deck.write_text("# stub deck\n", encoding="utf-8")
    out = cc.run_drc_crosscheck(
        _tiny_gds(tmp_path),
        "sky130",
        str(deck),
        native_kwargs={"allow_missing_host_tools": True},
    )
    assert out["reports"]["native"]["engine"] == "klayout"
    # Real decks and no mapping group: never an agreement.
    assert out["comparison"]["state"] == "not_comparable"
    _schema_validator().validate(out)
