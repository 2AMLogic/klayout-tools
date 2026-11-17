"""Issue #5863: `place-and-route --check --rerun` vs a path-scrubbed
committed report.

A consumer commits lint-clean evidence with output paths rewritten
repo-relative or to `~/`/`$PDK_ROOT/...`-form (what `env-provenance
lint-envelope` requires) and the resolving host's own
`provenance.pdk.source` label, while the fresh side of a `--rerun` diff is
the raw envelope. Before #5863 that diff could never exit 0 -- on any host.
These tests pin: scrub-shape independence (path fields compare by file
name), the `pdk.source` exclusion, and the identity checks that must
survive (a renamed or missing artifact, a real verdict change).

Reuses `test_place_and_route`'s stubbed-flow helpers: no OpenROAD needed.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from klayout_tools import place_and_route
from test_place_and_route import (
    _committed_pnr_report,
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# --------------------------------------------------------------------------- #
# unit: the canonicalizer
# --------------------------------------------------------------------------- #


def _canonical(report):
    return place_and_route._canonicalize_place_and_route_report_for_rerun_diff(report)


def test_canonicalize_reduces_path_fields_to_basenames():
    """Every scrub shape a consumer commits reduces to the same view iff the
    file name matches: absolute, repo-relative, `~/`-form, `$PDK_ROOT/...`,
    a `{path, scope}` envelope, Windows separators. `null` stays `null`."""
    shapes = [
        "/host/checkout/.klt/place-and-route/gcd.def",
        "evidence/pnr/gcd.def",
        "~/evidence/pnr/gcd.def",
        "$PDK_ROOT/sky130A/gcd.def",
        {"path": "evidence/pnr/gcd.def", "scope": "repo"},
        {"path": "~/evidence/pnr/gcd.def", "scope": "repo"},
        "evidence\\pnr\\gcd.def",
    ]
    expected = {
        "str": "gcd.def",
        "envelope": {"path": "gcd.def", "scope": "repo"},
    }
    for shape in shapes:
        view = _canonical({"def_path": shape})["def_path"]
        assert view == expected["envelope" if isinstance(shape, dict) else "str"]

    assert _canonical({"def_path": None}) == {"def_path": None}
    assert _canonical({"gds_path": "/a/b/gcd.gds"}) == _canonical(
        {"gds_path": "x/gcd.gds"}
    )
    assert _canonical({"gds_path": "/a/b/gcd.gds"}) != _canonical(
        {"gds_path": "x/other.gds"}
    )


def test_canonicalize_leaves_nulls_and_non_path_fields_alone():
    assert _canonical({"layer_map": {"path": None, "resolution": "none"}}) == {
        "layer_map": {"path": None, "resolution": "none"}
    }
    assert _canonical({"netlist": {"path": "e/gcd.v", "scope": "repo"}}) == {
        "netlist": {"path": "gcd.v", "scope": "repo"}
    }
    assert _canonical({"netlist": {"path": None, "scope": "external"}}) == {
        "netlist": {"path": None, "scope": "external"}
    }
    assert _canonical({"setup_violation_count": 3}) == {"setup_violation_count": 3}


def test_canonicalize_reaches_every_path_field_nesting():
    """The path-bearing keys recur nested (`power.placed.def_path`,
    `macros[].lef`/`.lib`, `spef_sta.*_path`, every `corner_runs[]` entry's
    `output_dir` and embedded report) -- the same key-name recursion
    `strip_keys` established."""
    report = {
        "power": {"placed": {"def_path": "/host/a/gcd.def"}},
        "macros": [
            {"lef": "/pdk/t/gcd.lef", "lib": "/pdk/l/gcd.lib", "cell": "gcd"},
        ],
        "spef_sta": {"spef_path": "/host/a/gcd.spef", "sdf_path": None},
        "corner_runs": [
            {
                "corner": "nom",
                "output_dir": "/host/.klt/place-and-route/nom",
                "def_path": "/host/.klt/place-and-route/nom/gcd.def",
                "netlist": {"path": "design/gcd.v", "scope": "repo"},
            },
        ],
    }
    assert _canonical(report) == {
        "power": {"placed": {"def_path": "gcd.def"}},
        "macros": [{"lef": "gcd.lef", "lib": "gcd.lib", "cell": "gcd"}],
        "spef_sta": {"spef_path": "gcd.spef", "sdf_path": None},
        "corner_runs": [
            {
                "corner": "nom",
                "output_dir": "nom",
                "def_path": "gcd.def",
                "netlist": {"path": "gcd.v", "scope": "repo"},
            },
        ],
    }
    # The canonicalization never mutates the caller's report.
    assert report["corner_runs"][0]["def_path"].startswith("/host/")


# --------------------------------------------------------------------------- #
# end-to-end: --rerun against a scrubbed committed report (stubbed flow)
# --------------------------------------------------------------------------- #


def _scrub_committed_evidence(committed: dict) -> dict:
    """A consumer's committed-evidence scrub over a copy, a different scrub
    shape per field family; also rewrites `provenance.pdk.source` to a
    resolution mechanism other than this host's (`PDK_ROOT` env)."""
    scrubbed = copy.deepcopy(committed)
    _scrub_output_paths(scrubbed)
    scrubbed.setdefault("provenance", {}).setdefault("pdk", {})["source"] = (
        "--pdk-root flag"
    )
    return scrubbed


def _scrub_output_paths(scrubbed: dict) -> None:
    """Rewrite `scrubbed`'s output-path fields in place: repo-relative or
    `~/`-form for artifacts, `$PDK_ROOT/...` for PDK files, repo-relative
    `path` members for `{path, scope}` envelopes. Basenames are the identity
    a `--rerun` diff can legitimately compare."""
    pdk_form = lambda p: f"$PDK_ROOT/sky130A/{os.path.basename(p)}"  # noqa: E731
    repo_form = lambda p: f"evidence/pnr/{os.path.basename(p)}"  # noqa: E731
    home_form = lambda p: f"~/evidence/pnr/{os.path.basename(p)}"  # noqa: E731
    envelope_form = lambda p: f"evidence/{os.path.basename(p)}"  # noqa: E731

    # (path to the dict holding the field, field, scrub form) -- one row per
    # output-path field family.
    rows = (
        ((("def_path",), "def_path", repo_form)),
        ((("unrouted_def_path",), "unrouted_def_path", repo_form)),
        ((("gds_path",), "gds_path", home_form)),
        ((("verilog_path",), "verilog_path", pdk_form)),
        ((("layer_map",), "path", pdk_form)),
        ((("netlist",), "path", envelope_form)),
        ((("provenance", "deck", "path"), "path", envelope_form)),
        ((("power", "placed"), "def_path", repo_form)),
        ((("spef_sta",), "spef_path", repo_form)),
        ((("spef_sta",), "sdf_path", repo_form)),
    )
    for path, key, form in rows:
        node: dict = scrubbed
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(key):
            node[key] = form(node[key])
    for macro in scrubbed.get("macros") or []:
        for key in ("lef", "lib"):
            if macro.get(key):
                macro[key] = pdk_form(macro[key])
    for run in scrubbed.get("corner_runs") or []:
        if run.get("output_dir"):
            run["output_dir"] = repo_form(run["output_dir"])


def test_rerun_matches_a_path_scrubbed_committed_report(tmp_path, monkeypatch):
    """The issue's headline scenario: a lint-clean (path-scrubbed, differing
    `pdk.source`) committed report whose flow reproduced exactly must give
    `status: "match"` -- on any host, this one included."""
    request_path, report_path = _committed_pnr_report(tmp_path, monkeypatch)
    committed = json.loads(Path(report_path).read_text())
    scrubbed = _scrub_committed_evidence(committed)
    assert scrubbed != committed  # the scrub actually changed something
    report_path = _write_report(tmp_path / "gcd.scrubbed.json", scrubbed)

    result = place_and_route.rerun_place_and_route_report(report_path, request_path)

    assert result["status"] == "match"
    assert result["drift"] == []


def _write_report(path: Path, report: dict) -> str:
    path.write_text(json.dumps(report), encoding="utf-8")
    return str(path)


def test_rerun_still_detects_verdict_drift_in_a_scrubbed_committed_report(
    tmp_path, monkeypatch
):
    """The same scrubbed-committed setup must still name a real verdict
    change -- and never a path field or the resolution provenance."""
    request_path, report_path = _committed_pnr_report(
        tmp_path, monkeypatch, setup_violations={"route": 0}
    )
    scrubbed = _scrub_committed_evidence(json.loads(Path(report_path).read_text()))
    scrubbed["setup_violation_count"] = 4
    report_path = _write_report(tmp_path / "gcd.scrubbed.json", scrubbed)

    _stub_openroad_success(monkeypatch, setup_violations={"route": 0})
    _stub_merge_def_to_gds(monkeypatch)
    result = place_and_route.rerun_place_and_route_report(report_path, request_path)

    assert result["status"] == "drifted"
    fields = {entry["field"] for entry in result["drift"]}
    assert "setup_violation_count" in fields
    assert not {f for f in fields if f.endswith(("_path", "output_dir"))}
    assert not {f for f in fields if "pdk.source" in f}


def test_rerun_detects_a_renamed_scrubbed_artifact(tmp_path, monkeypatch):
    """Basename comparison keeps the identity check: a committed report whose
    scrubbed `def_path` names a *different file* still drifts."""
    request_path, report_path = _committed_pnr_report(tmp_path, monkeypatch)
    scrubbed = _scrub_committed_evidence(json.loads(Path(report_path).read_text()))
    scrubbed["def_path"] = "evidence/pnr/gcd-renamed.def"
    report_path = _write_report(tmp_path / "gcd.renamed.json", scrubbed)

    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    result = place_and_route.rerun_place_and_route_report(report_path, request_path)

    assert result["status"] == "drifted"
    assert {entry["field"] for entry in result["drift"]} == {"def_path"}


def test_rerun_detects_an_artifact_going_missing(tmp_path, monkeypatch):
    """A path field the committed report recorded that the fresh run no
    longer produced (`null` <-> non-null) still drifts -- the "output went
    missing" signal basename normalization must not lose."""
    request_path, report_path = _committed_pnr_report(tmp_path, monkeypatch)
    scrubbed = _scrub_committed_evidence(json.loads(Path(report_path).read_text()))
    scrubbed["gds_path"] = None
    report_path = _write_report(tmp_path / "gcd.missing.json", scrubbed)

    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    result = place_and_route.rerun_place_and_route_report(report_path, request_path)

    assert result["status"] == "drifted"
    assert {entry["field"] for entry in result["drift"]} == {"gds_path"}


def test_rerun_unscrubbed_committed_report_is_unchanged(tmp_path, monkeypatch):
    """Behavior guard for #5863: a committed report kept byte-identical to
    the fresh envelope still matches (no regression for unscrubbed
    consumers); covered end-to-end by test_place_and_route's
    `test_rerun_place_and_route_report_clean_pass` -- this pins the
    canonicalizer's own idempotence on already-absolute values."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    fresh = place_and_route.run_place_and_route(request_path)
    report_path = _write_report(tmp_path / "gcd.pnr.json", fresh)

    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    result = place_and_route.rerun_place_and_route_report(report_path, request_path)

    assert result["status"] == "match"
    assert result["drift"] == []
