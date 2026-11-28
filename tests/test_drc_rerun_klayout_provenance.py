"""``klt drc --check --rerun`` over ``--engine klayout`` reports whose
provenance fields moved (issue #5660 review).

Issue #5660 made ``environment.klayout_binary`` unconditional on every
``--engine klayout`` run (a host-local path) and added
``provenance.klayout_module_version`` (the pip wheel axis, volatile exactly
like its sibling ``provenance.klayout_version``). ``--rerun`` diffs the union
of both sides' keys, so without an exclusion every klayout-engine report
committed *before* the change -- and every report re-run on a host that
installs the binary elsewhere -- would come back ``drifted`` (exit 3) with the
deck hash, input hash, verdict and counts all unchanged.

Kept out of ``tests/test_drc_klayout_engine.py``: that file sits at its
per-file size ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

import klayout_tools.drc_klayout_engine as engine_module
from helpers.subprocess_fakes import fake_completed
from klayout_tools.cli import main
from klayout_tools.drc_report_check import drc_rerun_result

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: Finding-free, but declaring two rule categories so `--expect-rule-categories
#: 2` grades the run `clean` (an empty deck is `coverage_unknown`, exit 4).
_CLEAN_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category><name>W.1</name><description>min width</description></category>
  <category><name>S.1</name><description>min space</description></category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
 </items>
</report-database>
"""


def _stub_klayout(monkeypatch) -> None:
    """``klayout -v`` answers a version; any other call is a deck launch that
    writes a finding-free report."""

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[1:] == ["-v"]:
            return fake_completed(stdout="KLayout 0.30.12\n")
        report = next(a for a in cmd if a.startswith("report=")).split("=", 1)[1]
        Path(report).write_text(_CLEAN_RDB, encoding="utf-8")
        return fake_completed()

    monkeypatch.setattr(engine_module.subprocess, "run", fake_run)


def _committed_report(
    tmp_path, monkeypatch, capsys, extra_argv=(), *, cells=("TOP",)
) -> tuple[Path, dict]:
    """Run ``klt drc --engine klayout`` once and return the report written to
    disk (a repo-scoped input, so ``--check``/``--rerun`` can locate it)."""
    _stub_klayout(monkeypatch)
    (tmp_path / ".git").mkdir(exist_ok=True)
    layout = kdb.Layout()
    layout.dbu = 0.001
    for cell in cells:
        layout.create_cell(cell).shapes(layout.layer(1, 0)).insert(
            kdb.Box(0, 0, 100, 1000)
        )
    gds = str(tmp_path / "test.gds")
    layout.write(gds)
    deck = tmp_path / "deck.lydrc"
    deck.write_text("# stub deck\n", encoding="utf-8")

    argv = [
        *("drc", gds, "--engine", "klayout", "--deck-file", str(deck)),
        *("--expect-rule-categories", "2"),
        *extra_argv,
    ]
    assert main([*argv, "--format", "json"]) == 0
    text = capsys.readouterr().out
    report_path = tmp_path / "report.json"
    report_path.write_text(text, encoding="utf-8")
    return report_path, json.loads(text)


def _rerun(report_path: Path, capsys) -> tuple[int, dict]:
    code = main(["drc", "--check", str(report_path), "--rerun", "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def _rewrite(report_path: Path, edit) -> None:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    edit(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")


def test_fresh_report_carries_the_fields_this_test_depends_on(
    tmp_path, monkeypatch, capsys
):
    """Guard against the regression tests below passing vacuously: the report
    must actually carry the two newly populated surfaces."""
    _, report = _committed_report(tmp_path, monkeypatch, capsys)
    assert report["environment"].keys() == {"klayout_binary"}
    assert "klayout_module_version" in report["provenance"]


def test_rerun_of_report_committed_before_the_new_fields_matches(
    tmp_path, monkeypatch, capsys
):
    """Scenario 1: a report with no ``environment`` block and no
    ``provenance.klayout_module_version`` -- what every klayout-engine report
    committed before #5660 looks like."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)

    def to_legacy(report):
        del report["environment"]
        del report["provenance"]["klayout_module_version"]

    _rewrite(report_path, to_legacy)

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert result["status"] == "match"
    assert code == 0


def _divergent_host(monkeypatch) -> None:
    """The host class #5660 exists for: the pip wheel (``0.30.13``), the
    standalone binary the stub answers ``-v`` with (``0.30.12``) and the
    build's expected version (``0.30.12``) -- wheel and application disagree,
    and only the *application* agrees with the build."""
    from klayout_tools import _provenance, build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: "0.30.13")
    monkeypatch.setattr(
        build_identity, "recorded_klayout_version_expected", lambda: "0.30.12"
    )


def _to_pre_5660(report: dict) -> None:
    """Rewrite a post-#5660 report into what the pre-#5660 code wrote on the
    divergent host: no ``environment``, no ``klayout_module_version``, and --
    the part a plain key deletion misses -- ``klayout_version`` and
    ``klayout_version_mismatch`` derived from the *wheel*, not the binary."""
    del report["environment"]
    del report["provenance"]["klayout_module_version"]
    report["provenance"]["klayout_version"] = "0.30.13"
    report["provenance"]["klayout_version_mismatch"] = True


def test_divergent_host_fresh_report_flips_the_mismatch_flag(
    tmp_path, monkeypatch, capsys
):
    """Anti-vacuity guard for the divergent-host tests below: the flag the
    legacy fixture carries (``True``, wheel-derived) must genuinely differ from
    what the fixed code writes on this host (``False``, binary-derived)."""
    _divergent_host(monkeypatch)
    _, report = _committed_report(tmp_path, monkeypatch, capsys)
    provenance = report["provenance"]
    assert provenance["klayout_version"] == "0.30.12"
    assert provenance["klayout_module_version"] == "0.30.13"
    assert provenance["klayout_version_mismatch"] is False


def test_rerun_of_pre_5660_report_on_divergent_host_matches(
    tmp_path, monkeypatch, capsys
):
    """Scenario 3 (the one the issue is about): the committed report was
    written by the pre-#5660 code on a host whose wheel and binary disagree,
    so its ``klayout_version_mismatch`` is ``true`` (wheel vs build). The fixed
    code computes ``false`` (binary vs build). Nothing in the evidence changed,
    so this is neither drift nor exit 3 -- and the flag is surfaced as
    ``not_compared`` rather than silently swallowed."""
    _divergent_host(monkeypatch)
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(report_path, _to_pre_5660)

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert result["status"] == "match"
    assert code == 0
    assert result["not_compared"] == [
        {"field": "provenance.klayout_version_mismatch", "committed": True}
    ]
    # The embedded fresh report is verbatim: it still carries its own flag.
    assert result["fresh"]["provenance"]["klayout_version_mismatch"] is False


def test_rerun_of_pre_5660_report_on_agreeing_host_matches(
    tmp_path, monkeypatch, capsys
):
    """Wheel, binary and build agree: the flag is ``false`` on both sides, and
    the legacy report re-runs as ``match``."""
    report_path, report = _committed_report(tmp_path, monkeypatch, capsys)
    wheel = report["provenance"]["klayout_module_version"]

    def to_legacy(r):
        _to_pre_5660(r)
        r["provenance"]["klayout_version"] = wheel
        r["provenance"]["klayout_version_mismatch"] = report["provenance"][
            "klayout_version_mismatch"
        ]

    _rewrite(report_path, to_legacy)

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert code == 0


def test_rerun_between_two_post_5660_reports_still_compares_the_flag(
    tmp_path, monkeypatch, capsys
):
    """Negative control: the ``false`` -> ``true`` signal the docs defend is
    kept where both reports use the binary-derived definition (both carry
    ``klayout_module_version``) -- the not-comparable rule is the pre/post
    boundary only."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["provenance"].update(klayout_version_mismatch=True),
    )

    code, result = _rerun(report_path, capsys)

    assert code == 3
    assert result["status"] == "drifted"
    assert result["drift"] == [
        {
            "field": "provenance.klayout_version_mismatch",
            "committed": True,
            "fresh": False,
        }
    ]
    assert "not_compared" not in result


def test_mismatch_flag_is_compared_when_the_fresh_report_has_no_module_version():
    """Negative control at the unit level: a curated-shaped pair (neither side
    carries ``klayout_module_version``; the wheel *is* the engine there, so the
    flag's definition never moved) must still drift on a changed flag."""
    committed = {"provenance": {"klayout_version_mismatch": True}}
    fresh = {"provenance": {"klayout_version_mismatch": False}}

    result = drc_rerun_result(report_path="r.json", committed=committed, fresh=fresh)

    assert result["status"] == "drifted"
    assert [d["field"] for d in result["drift"]] == [
        "provenance.klayout_version_mismatch"
    ]
    assert "not_compared" not in result


def test_boundary_rule_does_not_hide_a_flag_the_committed_report_never_carried():
    """The rule marks a *committed* flag not-compared; it must not widen into
    hiding a flag only the fresh report grew -- that is the ordinary
    schema-growth drift it always was."""
    committed = {"provenance": {"klayout_version": "0.30.13"}}
    fresh = {
        "provenance": {
            "klayout_version": "0.30.12",
            "klayout_module_version": "0.30.13",
            "klayout_version_mismatch": False,
        }
    }

    result = drc_rerun_result(report_path="r.json", committed=committed, fresh=fresh)

    assert "not_compared" not in result
    assert result["status"] == "drifted"
    assert [d["field"] for d in result["drift"]] == [
        "provenance.klayout_version_mismatch"
    ]


def test_rerun_ignores_a_different_host_local_binary_path(
    tmp_path, monkeypatch, capsys
):
    """Scenario 2: both sides carry the new fields; only the install prefix of
    the same KLayout differs between the committing and re-running host."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["environment"].update(klayout_binary="/opt/elsewhere/klayout"),
    )

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert code == 0


def test_rerun_ignores_a_different_klayout_module_version(
    tmp_path, monkeypatch, capsys
):
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["provenance"].update(klayout_module_version="0.0.1"),
    )

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert code == 0


def test_rerun_still_reports_real_evidence_drift(tmp_path, monkeypatch, capsys):
    """Negative control: the exclusions must not widen into masking a changed
    verdict-bearing field."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["provenance"]["deck"].update(content_hash="sha256:" + "0" * 64),
    )

    code, result = _rerun(report_path, capsys)

    assert code == 3
    assert result["status"] == "drifted"
    assert [d["field"] for d in result["drift"]] == ["provenance.deck.content_hash"]


def test_environment_block_is_dropped_only_when_the_exclusions_empty_it():
    """A future ``environment`` key is evidence until someone excludes it: the
    legacy-report allowance must not silently exempt the whole block."""
    committed = {"provenance": {}, "environment": {"klayout_binary": "/a/klayout"}}
    fresh = {
        "provenance": {"klayout_module_version": "0.30.12"},
        "environment": {"klayout_binary": "/b/klayout", "engine_flag": "x"},
    }

    result = drc_rerun_result(report_path="r.json", committed=committed, fresh=fresh)

    # Reported on the block (the committed side's was emptied and dropped), and
    # the host-local path the exclusions stripped is not echoed back in it.
    assert result["drift"] == [
        {"field": "environment", "committed": None, "fresh": {"engine_flag": "x"}}
    ]
    assert result["fresh"] == fresh  # the embedded fresh report is verbatim


# Issue #6986: the native-engine half of the rerun PDK fix. A report produced
# with `--pdk`/`--pdk-root` must re-verify in full mode; before the fix the
# rerun passed no PDK selection into `run_drc_klayout_engine`, so
# `provenance.pdk` always came back `fresh: null` and drifted.


def _make_pdk_install(tmp_path, *variants: str) -> str:
    """Minimal on-disk PDK install `find_pdk` can resolve -- the same shape
    `tests/test_drc.py`'s helper of that name builds, for several variants."""
    root = tmp_path / "pdk_install"
    for variant in variants:
        (root / variant / "libs.tech").mkdir(parents=True, exist_ok=True)
    return str(root)


def test_rerun_resolves_pdk_from_explicit_flags(tmp_path, monkeypatch, capsys):
    """A `--engine klayout` report produced with `--pdk`/`--pdk-root`
    re-verifies with the same explicit flags (issue #6986)."""
    root = _make_pdk_install(tmp_path, "sky130A")
    report_path, _ = _committed_report(
        tmp_path,
        monkeypatch,
        capsys,
        extra_argv=["--pdk", "sky130A", "--pdk-root", root],
    )

    code = main(
        [
            "drc",
            "--check",
            str(report_path),
            "--rerun",
            "--pdk",
            "sky130A",
            "--pdk-root",
            root,
            "--format",
            "json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert result["status"] == "match"
    assert result["fresh"]["provenance"]["pdk"]["name"] == "sky130A"


def test_rerun_resolves_pdk_from_the_environment_via_the_recorded_name(
    tmp_path, monkeypatch, capsys
):
    """No explicit flags on the rerun: `$PDK_ROOT` plus the variant name the
    committed report records re-resolve the install (issue #6986). The
    producing run also resolves through `$PDK_ROOT` (variant flag, no root
    flag), so both sides name the same discovery source."""
    monkeypatch.delenv("PDK", raising=False)
    root = _make_pdk_install(tmp_path, "sky130A")
    monkeypatch.setenv("PDK_ROOT", root)
    report_path, _ = _committed_report(
        tmp_path, monkeypatch, capsys, extra_argv=["--pdk", "sky130A"]
    )

    code, result = _rerun(report_path, capsys)

    assert code == 0
    assert result["status"] == "match"
    assert result["fresh"]["provenance"]["pdk"]["name"] == "sky130A"


def test_rerun_pdk_explicit_other_variant_drifts_on_the_pdk_name(
    tmp_path, monkeypatch, capsys
):
    """Explicitly selecting the other installed variant drifts and names the
    changed PDK (issue #6986), the native-engine counterpart of the curated
    test."""
    root = _make_pdk_install(tmp_path, "sky130A", "sky130B")
    report_path, _ = _committed_report(
        tmp_path,
        monkeypatch,
        capsys,
        extra_argv=["--pdk", "sky130A", "--pdk-root", root],
    )

    code = main(
        [
            "drc",
            "--check",
            str(report_path),
            "--rerun",
            "--pdk",
            "sky130B",
            "--pdk-root",
            root,
            "--format",
            "json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert code == 3
    assert result["status"] == "drifted"
    assert {entry["field"] for entry in result["drift"]} == {"provenance.pdk.name"}


def test_rerun_unresolvable_explicit_pdk_exits_one(tmp_path, monkeypatch, capsys):
    """An explicit `--pdk` that resolves to nothing is the normal exit-1
    error on the rerun (issue #6986), never a silent `fresh: null`."""
    monkeypatch.delenv("PDK", raising=False)
    root = _make_pdk_install(tmp_path, "sky130A")
    report_path, _ = _committed_report(
        tmp_path,
        monkeypatch,
        capsys,
        extra_argv=["--pdk", "sky130A", "--pdk-root", root],
    )

    code = main(
        [
            "drc",
            "--check",
            str(report_path),
            "--rerun",
            "--pdk",
            "no-such-variant",
            "--pdk-root",
            root,
            "--format",
            "json",
        ]
    )
    err = capsys.readouterr().err

    assert code == 1
    assert "PDK" in json.loads(err)["error"]["message"]
