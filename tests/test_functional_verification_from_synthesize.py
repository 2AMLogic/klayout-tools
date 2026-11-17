"""``options.from_synthesize`` (issue #5684): resolve + hash-gate the netlist a
``klt synthesize`` run produced. No simulator needed -- every case is a fixture
report plus a file on disk; the gate-passing case stops at a sentinel standing
in for the cocotb runner import."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from klayout_tools import functional_verification as fv

NETLIST = b"module gcd(input a, output y); assign y = a; endmodule\n"


class _PastGate(Exception):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def work(tmp_path, monkeypatch):
    (tmp_path / "test_gcd.py").write_text("# testbench\n")
    (tmp_path / "gcd_synth.v").write_bytes(NETLIST)

    def _stop(*_a, **_k):
        raise _PastGate

    monkeypatch.setattr(fv, "_import_runner", _stop)
    return tmp_path


def _report(work, **overrides):
    report = {
        "hdl_toplevel": "gcd",
        "netlist_path": str(work / "gcd_synth.v"),
        "netlist_sha256": _digest(NETLIST),
    }
    report.update(overrides)
    path = work / "report.json"
    path.write_text(json.dumps(report))
    return path


def _run(work, from_synthesize, *, sources=None, hdl_toplevel="gcd", **extra):
    request = {
        "hdl_toplevel": hdl_toplevel,
        "testbench": {"module": "test_gcd"},
        "options": {"from_synthesize": from_synthesize},
        **extra,
    }
    if sources is not None:
        request["sources"] = sources
    path = work / "request.json"
    path.write_text(json.dumps(request))
    return fv.run_functional_verification(str(path))


def _refused(work, from_synthesize, match, **kw):
    with pytest.raises(fv.FunctionalVerificationError, match=match):
        _run(work, from_synthesize, **kw)
    assert not (work / ".klt").exists(), "a build directory was created"


def test_matching_hash_passes_gate(work):
    _report(work)
    with pytest.raises(_PastGate):
        _run(work, {"report": "report.json"}, sources=[])


def test_block_and_netlist_prepended(work):
    from klayout_tools.functional_verification_from_synthesize import (
        resolve_from_synthesize,
    )

    _report(work)
    netlist, block = resolve_from_synthesize(
        {"from_synthesize": {"report": "report.json"}},
        str(work),
        "gcd",
        lambda p: {"path": os.path.basename(p), "scope": "repo"},
    )
    assert netlist == str(work / "gcd_synth.v")
    assert block["netlist_sha256"] == _digest(NETLIST)
    assert block["hash_check"]["match"] is True
    assert block["netlist"] == {"path": "gcd_synth.v", "scope": "repo"}


def test_stale_netlist_refused_naming_both_digests(work):
    _report(work)
    (work / "gcd_synth.v").write_bytes(NETLIST + b" ")
    _refused(
        work,
        {"report": "report.json"},
        f"{_digest(NETLIST)}.*{_digest(NETLIST + b' ')}",
        sources=[],
    )


def test_missing_netlist_sha256_fails_closed(work):
    path = _report(work)
    doc = json.loads(path.read_text())
    del doc["netlist_sha256"]
    path.write_text(json.dumps(doc))
    _refused(work, {"report": "report.json"}, "no netlist_sha256", sources=[])


def test_external_scope_needs_netlist_override(work):
    _report(work, netlist_path={"path": None, "scope": "external"})
    _refused(
        work,
        {"report": "report.json"},
        r"options\.from_synthesize\.netlist",
        sources=[],
    )
    with pytest.raises(_PastGate):
        _run(
            work,
            {"report": "report.json", "netlist": "gcd_synth.v"},
            sources=[],
        )


def test_override_wins_but_is_still_hash_checked(work):
    _report(work)
    (work / "other.v").write_bytes(NETLIST + b"// other\n")
    _refused(
        work,
        {"report": "report.json", "netlist": "other.v"},
        "netlist_sha256 mismatch",
        sources=[],
    )


def test_hdl_toplevel_mismatch(work):
    _report(work, hdl_toplevel="other")
    _refused(work, {"report": "report.json"}, "hdl_toplevel", sources=[])


def test_empty_sources_rule_unchanged_without_key(work):
    request = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": [],
    }
    path = work / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(
        fv.FunctionalVerificationError,
        match="request.sources must be a non-empty array of paths",
    ):
        fv.run_functional_verification(str(path))


@pytest.mark.parametrize(
    "value, match",
    [
        ("report.json", "must be a JSON object"),
        ({}, r"from_synthesize\.report"),
        ({"report": "report.json", "bogus": 1}, "unknown key.*bogus"),
        ({"report": "report.json", "netlist": 3}, r"from_synthesize\.netlist"),
    ],
)
def test_bad_shapes(work, value, match):
    _report(work)
    _refused(work, value, match, sources=[])


def test_report_not_json_or_not_synthesize(work):
    (work / "report.json").write_text("not json")
    _refused(work, {"report": "report.json"}, "not valid JSON", sources=[])
    (work / "report.json").write_text("{}")
    _refused(work, {"report": "report.json"}, "not a klt synthesize report", sources=[])


def test_from_synthesize_with_mutations_is_refused(work):
    """`options.from_synthesize` + `--mutations` is a request error (issue
    #5684): the mutation path's baseline run resolves netlist + sources via
    the synthesize report while each isolated per-mutant build resolves
    request.sources alone, so a mutant build would diverge from the
    baseline's own in a way nothing there would catch -- the same
    trace/coverage/initial_state/sdf refusal family, fifth member."""
    from klayout_tools.functional_verification_mutation import (
        run_functional_verification_with_mutations,
    )

    _report(work)
    (work / "dut.v").write_text("module dut(); wire a = 1; endmodule\n")
    (work / "proposals.json").write_text(
        json.dumps(
            {
                "schema": "klt.functional_verification.mutation_proposals/1",
                "scope": ["dut.v"],
                "proposals": [
                    {
                        "index": 1,
                        "category": "constant",
                        "file": "dut.v",
                        "line": 1,
                        "original_code": "wire a = 1;",
                        "mutated_code": "wire a = 9;",
                    }
                ],
            }
        )
    )
    request_path = work / "mutation-request.json"
    request_path.write_text(
        json.dumps(
            {
                "hdl_toplevel": "dut",
                "testbench": {"module": "test_gcd"},
                "options": {"from_synthesize": {"report": "report.json"}},
                "sources": ["dut.v"],
            }
        )
    )

    with pytest.raises(fv.FunctionalVerificationError, match="options.from_synthesize"):
        run_functional_verification_with_mutations(
            str(request_path), str(work / "proposals.json")
        )
