"""``klt sim --check`` netlist-evidence re-check (issue #5821)."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from klayout_tools.sim_errors import SimError
from klayout_tools.sim_report_check import check_sim_report
from klayout_tools.sim_staging import _normalized_netlist_text


def _raw(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(paths) -> str:
    digest = hashlib.sha256()
    for p in paths:
        digest.update(_normalized_netlist_text(p.read_text()).encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / ".git").mkdir()
    top = tmp_path / "top.cir"
    inc = tmp_path / "models_a.spice"
    inc.write_text(".model d d\n")
    top.write_text(".include models_a.spice\nr1 n1 0 1k\n")
    return tmp_path, top, inc


def _write(tmp_path, top, inc, **env_over):
    paths = [top, inc]
    env = {
        "netlist_closure": [
            {"path": p.name, "scope": "repo", "sha256": _raw(p)} for p in paths
        ],
        "netlist_identity": _identity(paths),
    }
    env.update(env_over)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"environment": env}))
    return str(report)


def _edit_report(report, mutate):
    data = json.loads(open(report).read())
    mutate(data["environment"])
    open(report, "w").write(json.dumps(data))


def test_clean_tree_matches(tree):
    tmp, top, inc = tree
    result = check_sim_report(_write(tmp, top, inc))
    assert result["status"] == "match"
    assert result["diagnosis"] == "intact"


def test_comment_edit_is_cosmetic(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    inc.write_text("* note\n.model d d\n")
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert result["diagnosis"] == "cosmetic"
    ident = [c for c in result["checks"] if c["field"].endswith("netlist_identity")]
    assert ident[0]["match"]


def test_value_edit_is_electrical(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    top.write_text(".include models_a.spice\nr1 n1 0 2k\n")
    assert check_sim_report(report)["diagnosis"] == "electrical"


def test_edit_inside_include_is_electrical(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    inc.write_text(".model d d is=1\n")
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert result["diagnosis"] == "electrical"


def test_repointed_include_caught_by_raw_hash(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    top.write_text(".include models_b.spice\nr1 n1 0 1k\n")
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert not result["checks"][0]["match"]


def test_normalization_change_named(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc, netlist_identity="sha256:" + "0" * 64)
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert result["diagnosis"] == "normalization_changed"
    assert "normalization" in result["note"]


def test_identity_error_never_matches(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc, netlist_identity=None, netlist_identity_error="boom")
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert result["checks"][-1]["error"] == "boom"


def test_closure_error_drifts(tmp_path):
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps({"environment": {"netlist_closure_error": "bad"}}))
    result = check_sim_report(str(rep))
    assert result["status"] == "drifted"
    assert result["checks"][0]["error"] == "bad"


def test_no_closure_at_all_never_matches(tmp_path):
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps({}))
    assert check_sim_report(str(rep))["status"] == "drifted"


def test_host_resolved_entry_skipped(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    _edit_report(
        report,
        lambda env: env["netlist_closure"].append(
            {"target": "$PDK/x.lib", "sha256": None, "unhashed_reason": "env_var"}
        ),
    )
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert "env_var" in result["skipped"][0]["reason"]


def test_external_scope_skipped(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)

    def mutate(env):
        env["netlist_closure"][1] = {
            "path": None,
            "scope": "external",
            "sha256": "ab" * 32,
        }

    _edit_report(report, mutate)
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert result["skipped"]


def test_missing_file_drifts(tree):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    os.remove(inc)
    result = check_sim_report(report)
    assert result["status"] == "drifted"
    assert "not found" in result["checks"][1]["error"]


def test_root_reanchors(tree, tmp_path_factory):
    tmp, top, inc = tree
    report = _write(tmp, top, inc)
    moved = tmp_path_factory.mktemp("elsewhere") / "report.json"
    moved.write_text(open(report).read())
    assert check_sim_report(str(moved), root=str(tmp))["status"] == "match"


def test_unreadable_report_raises(tmp_path):
    with pytest.raises(SimError):
        check_sim_report(str(tmp_path / "nope.json"))
