"""``klt signoff --attest 9``: a bench entry's ``sources`` (issue #5701).

The implementation modules a testbench is spread across: each unique source
path yields one ``present`` check with ``role: "source"``, attributed via
``testbenches`` to every bench that declared it. In its own file so both
item-9 test modules stay under the 500-line ratchet (the malformed-shapes
table would otherwise not fit next to the existing one).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    check_attestation,
    render_envelope,
)

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
_PINS = {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"ngspice": "42"},
}


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tb").mkdir()
    (repo / "attest").mkdir()
    for name in ("a", "b"):
        (repo / "tb" / f"{name}.spice").write_text(f"* {name}\n")
    return repo


def _inv(repo: Path, **fields) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 9,
        "pins": _PINS,
        **fields,
    }
    path = repo / "attest" / "item9.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _present(envelope: dict) -> list[dict]:
    return [c for c in envelope["attestation"]["checks"] if c["check"] == "present"]


def _repo_with_helpers(repo: Path) -> None:
    (repo / "tb" / "lib").mkdir()
    (repo / "tb" / "lib" / "helpers.py").write_text("# helpers\n")
    (repo / "tb" / "lib" / "analysers.py").write_text("# analysers\n")


def test_a_benchs_sources_are_hashed_as_source_checks(tmp_path):
    repo = _repo(tmp_path)
    _repo_with_helpers(repo)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "sources": ["tb/lib/helpers.py", "tb/lib/analysers.py"],
                }
            ],
        ),
    )

    present = _present(envelope)
    assert [(c["role"], c["path"]) for c in present] == [
        ("testbench", "tb/a.spice"),
        ("source", "tb/lib/analysers.py"),
        ("source", "tb/lib/helpers.py"),
    ]
    for source in (c for c in present if c["role"] == "source"):
        assert source["passed"] is True
        assert source["content_hash"].startswith("sha256:")
        assert source["testbenches"] == ["tb/a.spice"]
    assert envelope["status"] == "pass"


def test_a_source_named_by_two_benches_is_hashed_once_with_both(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tb" / "lib").mkdir()
    (repo / "tb" / "lib" / "helpers.py").write_text("# helpers\n")
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/b.spice",
                    "cold_start": {"argv": _OK},
                    "sources": ["tb/lib/helpers.py"],
                },
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "sources": ["tb/lib/helpers.py"],
                },
            ],
        ),
    )

    present = _present(envelope)
    assert [c["path"] for c in present if c["role"] == "testbench"] == [
        "tb/a.spice",
        "tb/b.spice",
    ]
    (source,) = [c for c in present if c["role"] == "source"]
    assert source["path"] == "tb/lib/helpers.py"
    assert source["content_hash"].startswith("sha256:")
    assert source["testbenches"] == ["tb/a.spice", "tb/b.spice"]


def test_a_missing_source_is_a_failing_check(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "sources": ["tb/lib/absent.py"],
                }
            ],
        ),
    )

    assert envelope["status"] == "fail"
    (source,) = [c for c in _present(envelope) if c["role"] == "source"]
    assert source["passed"] is False
    assert source["content_hash"] is None
    assert source["testbenches"] == ["tb/a.spice"]


@pytest.mark.parametrize("sources", [["tb/b.spice"], ["tb/a.spice"]])
def test_a_path_that_is_both_bench_and_source_is_refused(tmp_path, sources):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="already pinned as the testbench"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": _OK},
                        "sources": sources,
                    },
                    "tb/b.spice",
                ],
                cold_start={"argv": _OK},
            ),
        )


def test_sources_alone_do_not_satisfy_the_cold_start_requirement(tmp_path):
    repo = _repo(tmp_path)
    _repo_with_helpers(repo)
    with pytest.raises(AttestationError, match="cold_start"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[{"path": "tb/a.spice", "sources": ["tb/lib/helpers.py"]}],
            ),
        )


@pytest.mark.parametrize(
    "sources, message",
    [
        pytest.param("tb/lib.py", "sources", id="not-a-list"),
        pytest.param([], "sources", id="empty-list"),
        pytest.param([42], "sources", id="non-string-member"),
        pytest.param(["  "], "sources", id="blank-member"),
        pytest.param(["/etc/passwd"], "absolute", id="absolute-path"),
        pytest.param(["../outside.txt"], "outside the repository", id="dotdot-escape"),
        pytest.param(
            ["tb/lib.py", "tb/lib.py"], "more than once", id="duplicate-within-entry"
        ),
    ],
)
def test_malformed_sources_are_refused_naming_the_field(tmp_path, sources, message):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[
            {
                "path": "tb/a.spice",
                "cold_start": {"argv": _OK},
                "sources": sources,
            }
        ],
    )
    with pytest.raises(AttestationError, match=message):
        build_attestation(9, inventory)


def test_check_order_is_independent_of_listing_order(tmp_path):
    repo = _repo(tmp_path)
    _repo_with_helpers(repo)

    def build(testbenches):
        return build_attestation(
            9, _inv(repo, testbenches=testbenches, cold_start={"argv": _OK})
        )

    forward = build(
        [
            {"path": "tb/b.spice", "sources": ["tb/lib/helpers.py"]},
            {"path": "tb/a.spice", "sources": ["tb/lib/analysers.py"]},
        ]
    )
    backward = build(
        [
            {"path": "tb/a.spice", "sources": ["tb/lib/analysers.py"]},
            {"path": "tb/b.spice", "sources": ["tb/lib/helpers.py"]},
        ]
    )

    # The checks are order-independent; the envelopes differ only in the
    # inventory file's own recorded hash, which depends on listing order.
    assert [(c["role"], c["path"]) for c in _present(forward)] == [
        ("testbench", "tb/a.spice"),
        ("testbench", "tb/b.spice"),
        ("source", "tb/lib/analysers.py"),
        ("source", "tb/lib/helpers.py"),
    ]
    assert forward["attestation"]["checks"] == backward["attestation"]["checks"]


def test_sources_envelope_checks_clean_then_names_the_source_on_drift(tmp_path):
    repo = _repo(tmp_path)
    _repo_with_helpers(repo)
    helper = repo / "tb" / "lib" / "helpers.py"
    inventory = _inv(
        repo,
        testbenches=[
            {
                "path": "tb/a.spice",
                "cold_start": {"argv": _OK},
                "sources": ["tb/lib/helpers.py"],
            }
        ],
    )
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    committed.write_text(render_envelope(build_attestation(9, inventory)))

    assert check_attestation(9, inventory, str(committed))["status"] == "match"

    helper.write_text("# helpers, edited\n")
    result = check_attestation(9, inventory, str(committed))

    assert result["status"] != "match"
    (hashed,) = [
        e
        for e in result["drift"]
        if e["field"].startswith("attestation.checks.")
        and e["field"].endswith(".content_hash")
    ]
    assert hashed["check"] == {
        "role": "source",
        "check": "present",
        "path": "tb/lib/helpers.py",
    }


def test_text_output_names_a_source_bench(tmp_path, capsys):
    repo = _repo(tmp_path)
    _repo_with_helpers(repo)
    inventory = _inv(
        repo,
        testbenches=[
            {
                "path": "tb/a.spice",
                "cold_start": {"argv": _OK},
                "sources": ["tb/lib/helpers.py", "tb/lib/analysers.py"],
            }
        ],
    )
    capsys.readouterr()

    assert main(["signoff", "--attest", "9", "--inventory", inventory]) == 0

    out = capsys.readouterr().out
    assert "[PASS] source tb/lib/helpers.py present (bench: tb/a.spice)" in out
    assert "[PASS] source tb/lib/analysers.py present (bench: tb/a.spice)" in out
