"""``klt evidence canonicalize`` -- the ``--check … --rerun`` comparison view
as a committable document (issue #5541).

Built from small hand-written report dicts: no engine run, no PDK, no KLayout.
This module covers the registry/single-source structure, what the view
removes and keeps, determinism, and the CLI; the verb's one-directional
relationship with ``--rerun`` (equal views imply no drift; the directional
exceptions -- committed-only provenance #5866, DRC's #5660 boundary, the
``lvs`` pair tolerance) lives in
``test_evidence_canonicalize_rerun_equivalence.py``, split for the per-file
size ratchet. Shared fixtures: ``helpers/evidence_reports``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from helpers.evidence_reports import KINDS
from helpers.evidence_reports import base_report as _base_report
from helpers.evidence_reports import rerun_drift as _rerun_drift
from klayout_tools import evidence_canonicalize as ec
from klayout_tools.cli import main

_SRC = Path(__file__).resolve().parent.parent / "src" / "klayout_tools"

#: kind -> the module whose ``rerun_*_report`` must go through the registry.
_VERB_MODULES = {
    "drc": "drc_report_check.py",  # `drc_rerun_result` (#5660) owns drc's call
    "lvs": "lvs.py",
    "erc": "erc.py",
    "extract": "extract.py",
    "synthesize": "synthesize.py",
    "place-and-route": "place_and_route.py",
    "equiv": "equiv.py",
}


def _canonical_bytes(report: dict[str, Any], kind: str) -> str:
    return json.dumps(ec.canonicalize(report, kind), indent=2) + "\n"


def _rerun_status(kind: str, committed: dict[str, Any], fresh: dict[str, Any]) -> str:
    result = ec.rerun_result_for(kind, "r.json", committed=committed, fresh=fresh)
    return str(result["status"])


# --- Registry resolution and single-source-of-truth ---


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_resolves_to_live_objects(kind: str) -> None:
    """A renamed exclusion set / canonicalizer fails here, not as a runtime
    ``AttributeError`` inside the verb (the registry resolves lazily)."""
    spec = ec.kind_spec(kind)
    assert spec.kind == kind
    assert spec.exclude, "every kind excludes at least the volatile provenance"
    assert all(isinstance(path, tuple) for path in spec.exclude)
    assert spec.canonicalize is None or callable(spec.canonicalize)


def test_unknown_kind_is_a_named_error() -> None:
    with pytest.raises(ValueError, match="unknown report kind 'nope'"):
        ec.kind_spec("nope")


@pytest.mark.parametrize("kind", KINDS)
def test_no_verb_bypasses_the_registry(kind: str) -> None:
    """Each verb's ``--rerun`` goes through ``rerun_result_for`` and nowhere
    else -- the structural half of the single-source criterion: a direct
    ``build_rerun_result`` call would be a second kind-to-exclusion pairing
    the canonical view could fall behind."""
    source = (_SRC / _VERB_MODULES[kind]).read_text()
    assert "build_rerun_result(" not in source
    assert len(re.findall(r"\brerun_result_for\(", source)) == 1


def test_adding_a_bookkeeping_key_reaches_both_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One edit -- a key appended to ``synthesize.RERUN_BOOKKEEPING_KEYS`` --
    changes both ``--rerun``'s view and the canonical view."""
    from klayout_tools import synthesize

    committed = _base_report("synthesize")
    fresh = _base_report("synthesize")
    committed["area_um2"] = 34.5
    fresh["area_um2"] = 99.0

    assert "area_um2" in ec.canonicalize(committed, "synthesize")
    assert _rerun_status("synthesize", committed, fresh) == "drifted"

    monkeypatch.setattr(
        synthesize,
        "RERUN_BOOKKEEPING_KEYS",
        synthesize.RERUN_BOOKKEEPING_KEYS | {"area_um2"},
    )
    assert "area_um2" not in ec.canonicalize(committed, "synthesize")
    assert _rerun_status("synthesize", committed, fresh) == "match"


def test_adding_an_excluded_path_reaches_both_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The by-path half of the same property, on ``lvs``'s verb-local set."""
    from klayout_tools import lvs

    committed = _base_report("lvs")
    fresh = _base_report("lvs")
    committed["mismatch_count"] = 0
    fresh["mismatch_count"] = 3

    assert "mismatch_count" in ec.canonicalize(committed, "lvs")
    monkeypatch.setattr(
        lvs,
        "_LVS_RERUN_EXCLUDE_PATHS",
        lvs._LVS_RERUN_EXCLUDE_PATHS | {("mismatch_count",)},
    )
    assert "mismatch_count" not in ec.canonicalize(committed, "lvs")
    assert _rerun_status("lvs", committed, fresh) == "match"


# --- What is removed, what survives ---


@pytest.mark.parametrize("kind", KINDS)
def test_machine_local_pdk_fields_are_removed(kind: str) -> None:
    """``provenance.pdk.ambiguous_sources`` (#2564/#5182, the issue's "second,
    related field") is gone for every kind, with the tool-build provenance
    beside it. ``variant`` stays: it names what was verified against."""
    canonical = ec.canonicalize(_base_report(kind), kind)
    pdk = canonical["provenance"]["pdk"]
    assert not {"ambiguous_sources", "linked_trees", "version"} & set(pdk)
    assert "klt_version" not in canonical["provenance"]
    assert "timing" not in canonical
    assert pdk["variant"] == "sky130A"


def test_nested_bookkeeping_keys_are_removed() -> None:
    """``script_path``/``blackbox_path`` nested inside a verdict object, not
    only top-level -- the nesting a flat copy of the list gets wrong."""
    canonical = ec.canonicalize(_base_report("synthesize"), "synthesize")
    assert "script_path" not in canonical
    assert "run_id" not in canonical
    assert canonical["equivalence"] == {"status": "equivalent", "artifacts": {}}
    assert canonical["macros"] == [{"name": "sram", "lef": "sram.lef"}]
    assert canonical["instance_count"] == 12


def test_flow_verb_run_scoped_keys_are_removed() -> None:
    pnr = ec.canonicalize(_base_report("place-and-route"), "place-and-route")
    assert "engine_logs" not in pnr and pnr["stages"]["detailed_route"] == {}
    assert pnr["def_path"] == "block.def"  # file name only, per #5863
    assert pnr["wns_ns"] == -0.1

    equiv = ec.canonicalize(_base_report("equiv"), "equiv")
    assert not {"elapsed_s", "resume", "yosys_binary"} & set(equiv)
    assert equiv["counterexample"] == {"found": False, "simulation": {}}


def test_extract_anonymous_net_spellings_canonicalize_equal() -> None:
    """Two builds numbering the same anonymous net differently compare
    equal, as ``klt extract --rerun`` already treats them."""
    committed = _base_report("extract")
    fresh = _base_report("extract")
    fresh["devices"][0]["nets"]["S"] = "\\$42"
    fresh["nets"][0]["name"] = "\\$42"
    fresh["nets"][0]["net_id"] = 99

    assert ec.canonicalize(committed, "extract") == ec.canonicalize(fresh, "extract")
    assert not _rerun_drift("extract", committed, fresh)

    renamed = _base_report("extract")
    renamed["devices"][0]["nets"]["D"] = "vout_b"
    assert ec.canonicalize(committed, "extract") != ec.canonicalize(renamed, "extract")


@pytest.mark.parametrize("kind", KINDS)
def test_verdict_fields_survive_with_their_values(kind: str) -> None:
    """Every key the kind neither excludes nor drops keeps its value, save
    place-and-route's ``def_path`` (rewritten to its file name, #5863)."""
    spec = ec.kind_spec(kind)
    report = _base_report(kind)
    removed = {path[0] for path in spec.exclude if len(path) == 1}
    canonical = ec.canonicalize(report, kind)
    for key, value in report.items():
        if key in removed or key not in canonical or key == "def_path":
            continue
        if isinstance(value, (dict, list)):
            continue
        assert canonical[key] == value, key
    assert canonical["schema_version"] == 1
    assert canonical["status"] == "clean"


# --- Determinism ---


@pytest.mark.parametrize("kind", KINDS)
def test_idempotent_and_byte_stable(kind: str) -> None:
    report = _base_report(kind)
    once = ec.canonicalize(report, kind)
    assert ec.canonicalize(once, kind) == once
    assert _canonical_bytes(once, kind) == _canonical_bytes(report, kind)


@pytest.mark.parametrize("kind", KINDS)
def test_key_order_does_not_leak_into_the_output(kind: str) -> None:
    report = _base_report(kind)
    shuffled = dict(reversed(list(report.items())))
    assert _canonical_bytes(shuffled, kind) == _canonical_bytes(report, kind)


def test_report_without_any_excluded_key_is_only_reserialized() -> None:
    report = {"schema_version": 1, "status": "clean", "violation_count": 0}
    assert ec.canonicalize(report, "drc") == report


# --- Existing --rerun payload is unchanged ---


@pytest.mark.parametrize("kind", KINDS)
def test_rerun_payload_embeds_the_un_canonicalized_fresh(kind: str) -> None:
    """``rerun_result_for`` keeps ``build_rerun_result``'s contract: the
    embedded ``fresh`` is the real report, never the canonical view."""
    committed = _base_report(kind)
    fresh = _base_report(kind)
    result = ec.rerun_result_for(
        kind, "evidence/report.json", committed=committed, fresh=fresh
    )
    assert result["mode"] == "rerun"
    assert result["report"] == "evidence/report.json"
    assert result["status"] == "match"
    assert result["fresh"] == fresh


def test_lvs_extra_exclude_is_pair_dependent_and_additive() -> None:
    """``klt lvs --rerun``'s pair-dependent tolerance rides on
    ``extra_exclude``, leaving the static set the canonical view uses."""
    committed = _base_report("lvs")
    fresh = _base_report("lvs")
    fresh["options"] = {"anchor_top_level_pins": True}
    tolerant = ec.rerun_result_for(
        "lvs",
        "r.json",
        committed=committed,
        fresh=fresh,
        extra_exclude={("options",)},
    )

    assert _rerun_status("lvs", committed, fresh) == "drifted"
    assert tolerant["status"] == "match"
    assert ("options",) not in ec.kind_spec("lvs").exclude


# --- CLI ---


@pytest.mark.parametrize("kind", KINDS)
def test_cli_emits_the_canonical_document(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _base_report(kind)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))

    assert main(["evidence", "canonicalize", "--kind", kind, str(path)]) == 0
    out = capsys.readouterr().out
    assert out == _canonical_bytes(report, kind)
    assert json.loads(out) == ec.canonicalize(report, kind)


def test_cli_round_trips_through_stdin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`… r.json | … -` reproduces the first command's bytes."""
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_base_report("synthesize")))
    assert main(["evidence", "canonicalize", "--kind", "synthesize", str(path)]) == 0
    first = capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(first))
    assert main(["evidence", "canonicalize", "--kind", "synthesize", "-"]) == 0
    assert capsys.readouterr().out == first


def test_cli_text_format_is_a_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_base_report("drc")))
    argv = ["evidence", "canonicalize", "--kind", "drc", str(path)]
    assert main([*argv, "--format", "text"]) == 0
    assert "top-level field(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "contents, expected",
    [
        (None, "file not found"),
        ("{not json", "is not valid JSON"),
        ("[]", "must be a JSON object"),
    ],
)
def test_cli_error_envelope_and_exit_1(
    contents: str | None,
    expected: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "report.json"
    if contents is not None:
        path.write_text(contents)
    assert main(["evidence", "canonicalize", "--kind", "drc", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["command"] == "evidence canonicalize"
    assert expected in payload["error"]["message"]


@pytest.mark.parametrize(
    "contents",
    [
        '{"nets": [{"name": "VDD"}, {"name": null}], "devices": []}',
        '{"nets": [{"name": "VDD"}], "devices": [], "parasitics": '
        '{"nets": [{"net": "VDD"}, {"net": null}]}}',
        '{"nets": [], "devices": [{"name": "M1", "nets": ["S"]}]}',
    ],
)
def test_cli_malformed_extract_shape_is_the_error_envelope(
    contents: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A JSON object that is not shaped like an extract report exits 1 with
    the shared error envelope, never a raw TypeError/AttributeError
    traceback (extract's canonicalizer reads field shapes no other kind's
    does: null net names, mixed string/null parasitics nets, a list-valued
    ``devices[].nets``)."""
    path = tmp_path / "report.json"
    path.write_text(contents)
    assert main(["evidence", "canonicalize", "--kind", "extract", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["command"] == "evidence canonicalize"
    assert "not a well-formed extract" in payload["error"]["message"]


def test_cli_unknown_kind_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("{}")
    with pytest.raises(SystemExit) as excinfo:
        main(["evidence", "canonicalize", "--kind", "signoff", str(path)])
    assert excinfo.value.code == 2
