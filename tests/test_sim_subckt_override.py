"""``netlist_overrides.subckt`` -- replace a named ``.subckt`` of the include
closure (issue #3919). Deck-text assertions only; no real SPICE run."""

import json
import re
import subprocess

import pytest

from klayout_tools import sim
from klayout_tools import sim_subckt_override as ovr

LIB = (
    "* heavy array\n"
    ".param w=1\n"
    ".SUBCKT Big in out\n"
    "+ vdd\n"
    ".subckt inner a b\n"
    "R1 a b 1k\n"
    ".ends inner\n"
    "Xi in out inner\n"
    ".ENDS Big\n"
    ".subckt other a b\n"
    "R2 a b 2k\n"
    ".ends\n"
)
REPLACEMENT = ".subckt big in out vdd\nR9 in out 9k\n.ends big\n"
TOP = '.include "lib/heavy.sp"\nXb a b vdd Big\nXo a b other\n.end\n'


def _write(tmp_path, top=TOP, lib=LIB):
    (tmp_path / "lib").mkdir(exist_ok=True)
    (tmp_path / "lib" / "heavy.sp").write_text(lib)
    (tmp_path / "reduced.sp").write_text(REPLACEMENT)
    (tmp_path / "top.sp").write_text(top)
    return str(tmp_path / "top.sp")


def _apply(tmp_path, name="BIG", top=TOP, lib=LIB):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    netlist = _write(tmp_path, top, lib)
    specs = ovr.parse_spec(
        {"subckt": [{"name": name, "replacement": "reduced.sp"}]}, str(tmp_path)
    )
    return ovr.apply_overrides(netlist, specs, work_dir=str(work))


def test_strips_only_the_target_block_and_repoints_include(tmp_path):
    path, report = _apply(tmp_path)

    top = open(path).read()
    copy = re.search(r'\.include "([^"]+heavy\.sp)"', top).group(1)
    assert copy.startswith(str(tmp_path / "work"))
    stripped = open(copy).read()
    # nested .subckt/.ends and `+` continuation went with the block ...
    assert "Big" not in stripped.replace("heavy array", "")
    assert "inner" not in stripped and "R1" not in stripped
    # ... the rest is untouched, and the replacement is included in place
    assert ".param w=1" in stripped and ".subckt other a b" in stripped
    assert f'.include "{tmp_path / "reduced.sp"}"' in stripped
    assert "Xb a b vdd Big" in top
    # the caller's file is unchanged
    assert (tmp_path / "lib" / "heavy.sp").read_text() == LIB
    (entry,) = report
    assert entry["name"] == "BIG"
    assert entry["original_sha256"] != entry["stripped_sha256"]
    assert len(entry["replacement_sha256"]) == 64


def test_target_in_top_level_netlist(tmp_path):
    top = ".subckt big a b\nR1 a b 1\n.ends\nXb a b big\n.end\n"
    path, _ = _apply(tmp_path, top=top)

    text = open(path).read()
    assert "R1 a b 1" not in text and "Xb a b big" in text
    assert "reduced.sp" in text


def test_relative_includes_of_copy_are_absolutized(tmp_path):
    lib = '.include "sub/leaf.sp"\n' + LIB
    (tmp_path / "lib" / "sub").mkdir(parents=True)
    (tmp_path / "lib" / "sub" / "leaf.sp").write_text(".param z=1\n")
    path, _ = _apply(tmp_path, lib=lib)

    copy = re.search(r'\.include "([^"]+heavy\.sp)"', open(path).read()).group(1)
    assert f'.include "{tmp_path / "lib" / "sub" / "leaf.sp"}"' in open(copy).read()


def test_unknown_name_is_refused(tmp_path):
    with pytest.raises(ovr.OverrideError, match="not defined"):
        _apply(tmp_path, name="nope")


def test_ambiguous_name_is_refused(tmp_path):
    top = ".subckt big a b\n.ends\n" + TOP
    with pytest.raises(ovr.OverrideError, match="ambiguous"):
        _apply(tmp_path, top=top)


def test_name_only_in_instance_lines_is_not_a_definition(tmp_path):
    with pytest.raises(ovr.OverrideError, match="not defined"):
        _apply(tmp_path, name="inner2", top=TOP + "Xq a b inner2\n")


def test_replacement_must_define_the_subckt(tmp_path):
    netlist = _write(tmp_path)
    (tmp_path / "reduced.sp").write_text(".subckt else a b\n.ends\n")
    specs = ovr.parse_spec(
        {"subckt": [{"name": "big", "replacement": "reduced.sp"}]}, str(tmp_path)
    )
    with pytest.raises(ovr.OverrideError, match="does not define"):
        ovr.apply_overrides(netlist, specs, work_dir=str(tmp_path))


@pytest.mark.parametrize(
    "spec, match",
    [
        ([], "must be an object"),
        ({}, "non-empty list"),
        ({"subckt": [{"name": "a"}]}, "exactly"),
        ({"subckt": [{"name": "a b", "replacement": "x"}]}, "single subcircuit"),
        ({"subckt": [{"name": "a", "replacement": "missing.sp"}]}, "not found"),
        ({"model": []}, "unsupported key"),
    ],
)
def test_invalid_specs_are_rejected(tmp_path, spec, match):
    with pytest.raises(ovr.OverrideError, match=match):
        ovr.parse_spec(spec, str(tmp_path))


def _request(tmp_path, **extra):
    netlist = _write(tmp_path)
    doc = {
        "netlist": "top.sp",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "netlist_overrides": {"subckt": [{"name": "big", "replacement": "reduced.sp"}]},
        **extra,
    }
    assert netlist
    path = tmp_path / "request.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_run_sim_refuses_offhost_backend(tmp_path):
    with pytest.raises(sim.SimError, match="only supported on local backends"):
        sim.run_sim(_request(tmp_path), backend="batch")


def test_run_sim_unknown_name_is_a_sim_error(tmp_path):
    request = _request(tmp_path)
    doc = json.loads(open(request).read())
    doc["netlist_overrides"]["subckt"][0]["name"] = "nope"
    open(request, "w").write(json.dumps(doc))
    with pytest.raises(sim.SimError, match="not defined"):
        sim.run_sim(request, backend="local")


def _fake_ngspice(monkeypatch, decks):
    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout, cwd=None, **_kw):
        if "-o" not in cmd:
            return real_run(
                cmd,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                cwd=cwd,
                **_kw,
            )
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write("")
        decks.append(open(next(c for c in cmd if str(c).endswith(".cir"))).read())
        return subprocess.CompletedProcess(cmd, 0, "** ngspice-99\n", "")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_sim_runs_overridden_deck_and_records_it(tmp_path, monkeypatch):
    decks: list[str] = []
    _fake_ngspice(monkeypatch, decks)

    report = sim.run_sim(_request(tmp_path), backend="local")

    (entry,) = report["environment"]["netlist_overrides"]["subckt"]
    assert entry["name"] == "big"
    assert entry["original_sha256"] != entry["stripped_sha256"]
    include = re.search(r"^\.include (\S+)", decks[0], re.MULTILINE).group(1)
    assert "klt-sim-" in include  # the stripped copy, not the caller's file
    assert (tmp_path / "lib" / "heavy.sp").read_text() == LIB


def test_without_the_field_the_report_is_unchanged(tmp_path, monkeypatch):
    request = _request(tmp_path)
    doc = json.loads(open(request).read())
    del doc["netlist_overrides"]
    open(request, "w").write(json.dumps(doc))
    decks: list[str] = []
    _fake_ngspice(monkeypatch, decks)

    report = sim.run_sim(request, backend="local")

    assert "netlist_overrides" not in report["environment"]
    assert str(tmp_path / "top.sp") in decks[0] and "klt-sim-" not in decks[0]
