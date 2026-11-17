"""``klt signoff --attest 9``: per-bench cold start, unbenched rows, stderr
progress (issue #4848), and declared dependency edges (issue #5850)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    render_envelope,
)

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
_BAD = [sys.executable, "-c", "import sys; sys.exit(1)"]
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


def _commands(envelope: dict) -> list[dict]:
    return [c for c in envelope["attestation"]["checks"] if c["check"] == "command"]


def test_per_bench_cold_start_names_the_failing_bench(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[
            {"path": "tb/a.spice", "cold_start": {"argv": _OK}},
            {"path": "tb/b.spice", "cold_start": {"argv": _BAD}},
        ],
    )

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "fail"
    by_bench = {c["testbench"]: c["passed"] for c in _commands(envelope)}
    assert by_bench == {"tb/a.spice": True, "tb/b.spice": False}


def test_mixed_entries_with_top_level_cold_start(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=["tb/a.spice", {"path": "tb/b.spice", "cold_start": {"argv": _OK}}],
        cold_start={"argv": _OK},
    )

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "pass"
    commands = _commands(envelope)
    assert [c.get("testbench") for c in commands] == [None, "tb/b.spice"]


def test_old_form_envelope_has_no_new_fields(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(repo, testbenches=["tb/b.spice", "tb/a.spice"], cold_start={"argv": _OK}),
    )

    roles = [c["role"] for c in envelope["attestation"]["checks"]]
    # The dependency_binding rows (one per member, #5850) are additive; every
    # pre-#5850 check keeps its place and shape.
    assert roles == [
        "testbench",
        "testbench",
        "cold_start",
        "testbench",
        "testbench",
        "pdk",
        "tools",
    ]
    assert all("testbench" not in c for c in _commands(envelope))


@pytest.mark.parametrize(
    "testbenches",
    [
        ["tb/a.spice", "tb/b.spice"],
        [{"path": "tb/a.spice", "cold_start": {"argv": _OK}}, "tb/b.spice"],
    ],
)
def test_missing_cold_start_is_refused(tmp_path, testbenches):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="cold_start"):
        build_attestation(9, _inv(repo, testbenches=testbenches))


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"cold_start": {"argv": _OK}}, "path"),
        ({"path": "tb/a.spice", "cold_start": {"argv": []}}, "argv"),
        ({"path": "tb/a.spice", "cold_start": {"argv": _OK}, "x": 1}, "unknown"),
    ],
)
def test_malformed_bench_entry_is_refused(tmp_path, entry, message):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match=message):
        build_attestation(9, _inv(repo, testbenches=[entry]))


def test_duplicate_bench_path_is_refused(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="more than once"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    "tb/a.spice",
                    {"path": "tb/a.spice", "cold_start": {"argv": _OK}},
                ],
                cold_start={"argv": _OK},
            ),
        )


def test_unbenched_rows_surface_as_passing_checks(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=["tb/a.spice"],
            cold_start={"argv": _OK},
            unbenched_rows=[{"row": "R9", "issue": 12}, {"row": "R2", "issue": 7}],
        ),
    )

    assert envelope["status"] == "pass"
    rows = [
        c for c in envelope["attestation"]["checks"] if c["check"] == "unbenched_row"
    ]
    assert rows == [
        {
            "check": "unbenched_row",
            "role": "row",
            "row": "R2",
            "issue": 7,
            "passed": True,
        },
        {
            "check": "unbenched_row",
            "role": "row",
            "row": "R9",
            "issue": 12,
            "passed": True,
        },
    ]


@pytest.mark.parametrize(
    "rows",
    [
        "R1",
        [{"row": "R1"}],
        [{"row": "", "issue": 1}],
        [{"row": "R1", "issue": 0}],
        [{"row": "R1", "issue": True}],
        [{"row": "R1", "issue": 1, "extra": 1}],
        [{"row": "R1", "issue": 1}, {"row": "R1", "issue": 2}],
    ],
)
def test_malformed_unbenched_rows_are_refused(tmp_path, rows):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK}, unbenched_rows=rows
    )
    with pytest.raises(AttestationError, match="unbenched"):
        build_attestation(9, inventory)


def test_progress_goes_to_stderr_and_stdout_stays_one_envelope(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[{"path": "tb/a.spice", "cold_start": {"argv": _OK}}],
        cold_start={"argv": _OK},
    )

    assert (
        main(["signoff", "--attest", "9", "--inventory", inventory, "--format", "json"])
        == 0
    )

    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "running cold_start ..." in captured.err
    assert "running cold_start tb/a.spice ..." in captured.err
    assert "cold_start tb/a.spice ok in " in captured.err


def test_local_only_bench_is_skipped_under_check(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[
            {"path": "tb/a.spice", "cold_start": {"argv": _BAD, "local_only": True}}
        ],
    )
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    # Attest-time: the local-only bench passes when the command passes.
    ok_inventory = _inv(
        repo,
        testbenches=[
            {"path": "tb/a.spice", "cold_start": {"argv": _OK, "local_only": True}}
        ],
    )
    committed.write_text(render_envelope(build_attestation(9, ok_inventory)))
    # --check must not run the (now failing) command; it carries the outcome.
    code = main(
        [
            "signoff",
            "--attest",
            "9",
            "--inventory",
            inventory,
            "--check",
            str(committed),
            "--format",
            "json",
        ]
    )
    assert code == 0


def _pdk_pin_check(envelope: dict) -> dict:
    return next(
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "pin" and c["role"] == "pdk"
    )


def _with_pdk(tmp_path, pdk) -> dict:
    repo = _repo(tmp_path)
    pins = {"tools": {"iverilog": "12.0"}}
    if pdk is not None:
        pins["pdk"] = pdk
    inventory = _inv(
        repo,
        pins=pins,
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK},
    )
    return build_attestation(9, inventory)


def test_no_pdk_form_passes(tmp_path):
    envelope = _with_pdk(tmp_path, {"none": True, "reason": "behavioral RTL only"})

    check = _pdk_pin_check(envelope)
    assert check["passed"] is True
    assert check["value"] == {"none": True, "reason": "behavioral RTL only"}
    assert envelope["status"] == "pass"


def test_absent_pdk_still_fails(tmp_path):
    envelope = _with_pdk(tmp_path, None)

    assert _pdk_pin_check(envelope)["passed"] is False
    assert envelope["status"] == "fail"


@pytest.mark.parametrize(
    "pdk",
    [
        {"none": True},
        {"none": True, "reason": ""},
        {"none": True, "reason": "  "},
        {"none": True, "reason": 5},
        {"none": True, "reason": "x", "name": "sky130A", "revision": "abc"},
        {"none": "yes", "reason": "x"},
    ],
)
def test_malformed_no_pdk_form_fails_without_crashing(tmp_path, pdk):
    envelope = _with_pdk(tmp_path, pdk)

    assert _pdk_pin_check(envelope)["passed"] is False
    assert envelope["status"] == "fail"


# --------------------------------------------------------------------------- #
# documented_argv: a cold start that only validates its bench (issue #5463)
# --------------------------------------------------------------------------- #

_DOCUMENTED = ["klt", "sim", "sim/mc.json", "--format", "json"]


def _never_run(repo: Path) -> tuple[Path, list[str]]:
    """A marker path and an argv that creates it -- declared as
    ``documented_argv``, it must never execute."""
    marker = repo / "documented-ran.txt"
    return marker, [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]


def _cold_start_check(envelope: dict) -> dict:
    return next(c for c in _commands(envelope) if c["role"] == "cold_start")


def test_top_level_documented_argv_is_recorded_and_not_run(tmp_path):
    repo = _repo(tmp_path)
    marker, documented = _never_run(repo)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=["tb/a.spice"],
            cold_start={"argv": _OK, "documented_argv": documented},
        ),
    )

    assert envelope["status"] == "pass"
    check = _cold_start_check(envelope)
    assert check["argv"] == _OK  # the declared command still ran and gated
    assert check["exit_status"] == 0
    assert check["reproduces"] == "validates"
    assert check["documented_argv"] == documented
    assert not marker.exists()


def test_per_bench_documented_argv_is_recorded_and_not_run(tmp_path):
    repo = _repo(tmp_path)
    marker, documented = _never_run(repo)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK, "documented_argv": documented},
                },
                {"path": "tb/b.spice", "cold_start": {"argv": _OK}},
            ],
        ),
    )

    assert envelope["status"] == "pass"
    by_bench = {c["testbench"]: c for c in _commands(envelope)}
    assert by_bench["tb/a.spice"]["documented_argv"] == documented
    assert by_bench["tb/a.spice"]["reproduces"] == "validates"
    assert "reproduces" not in by_bench["tb/b.spice"]
    assert not marker.exists()


def test_a_failing_declared_argv_still_fails_with_documented_argv(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=["tb/a.spice"],
            cold_start={"argv": _BAD, "documented_argv": _DOCUMENTED},
        ),
    )

    assert envelope["status"] == "fail"
    assert _cold_start_check(envelope)["passed"] is False


def test_undeclared_documented_argv_leaves_the_envelope_unchanged(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK})
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    committed.write_text(render_envelope(build_attestation(9, inventory)))

    assert "reproduces" not in committed.read_text()
    assert set(_cold_start_check(build_attestation(9, inventory))) == {
        "check",
        "role",
        "argv",
        "cwd",
        "exit_status",
        "passed",
    }
    code = main(
        ["signoff", "--attest", "9", "--inventory", inventory]
        + ["--check", str(committed), "--format", "json"]
    )
    assert code == 0


@pytest.mark.parametrize(
    "documented", ["klt sim", [], ["klt", ""], ["klt", 1], {"argv": ["klt"]}, None]
)
def test_malformed_documented_argv_is_refused(tmp_path, documented):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK, "documented_argv": documented},
    )
    with pytest.raises(AttestationError, match="documented_argv"):
        build_attestation(9, inventory)


def test_documented_argv_outside_item_9_is_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 10,
        "hygiene": ["tb/a.spice"],
        "checks": [
            {"name": "audit", "argv": _OK, "documented_argv": _DOCUMENTED},
        ],
    }
    path = repo / "attest" / "item10.json"
    path.write_text(json.dumps(inventory))

    with pytest.raises(AttestationError, match="documented_argv.*item 9"):
        build_attestation(10, str(path))


def test_documented_argv_on_an_item_1_derived_check_is_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 1,
        "sources": ["tb/a.spice"],
        "derived": [
            {
                "path": "tb/b.spice",
                "generator": "klt netlist",
                "check": {"argv": _OK, "documented_argv": _DOCUMENTED},
            }
        ],
    }
    path = repo / "attest" / "item1.json"
    path.write_text(json.dumps(inventory))

    with pytest.raises(AttestationError, match="documented_argv"):
        build_attestation(1, str(path))


def test_text_output_marks_a_validation_only_cold_start(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK, "documented_argv": _DOCUMENTED},
    )
    capsys.readouterr()

    assert main(["signoff", "--attest", "9", "--inventory", inventory]) == 0

    out = capsys.readouterr().out
    assert f"(validates only; documented: {' '.join(_DOCUMENTED)})" in out


# --------------------------------------------------------------------------- #
# depends_on: declared dependency edges (issue #5850)
# --------------------------------------------------------------------------- #


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _dep_checks(envelope: dict) -> list[dict]:
    return [
        c
        for c in envelope["attestation"]["checks"]
        if c["role"] == "testbench_dependency"
    ]


def _binding_checks(envelope: dict) -> list[dict]:
    return [
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "dependency_binding"
    ]


def _helper(repo: Path, text: str = ".model nfet n\n") -> Path:
    helper = repo / "models" / "helpers.inc"
    helper.parent.mkdir(exist_ok=True)
    helper.write_text(text)
    return helper


def test_a_declared_dependency_is_hashed_and_names_its_bench(tmp_path):
    repo = _repo(tmp_path)
    helper = _helper(repo)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "depends_on": ["models/helpers.inc"],
                },
                "tb/b.spice",
            ],
            cold_start={"argv": _OK},
        ),
    )

    assert envelope["status"] == "pass"
    (check,) = _dep_checks(envelope)
    assert check["check"] == "present"
    assert check["testbench"] == "tb/a.spice"
    assert check["path"] == "models/helpers.inc"
    assert check["passed"] is True
    assert check["content_hash"] == _hash(helper)
    bindings = {c["testbench"]: c["declared"] for c in _binding_checks(envelope)}
    assert bindings == {"tb/a.spice": 1, "tb/b.spice": 0}
    assert all(c["passed"] for c in _binding_checks(envelope))


def test_editing_a_declared_dependency_drifts_the_check(tmp_path):
    """The issue's reproduction, inverted: before #5850 an edited helper
    left ``--check`` green (exit 0); now the drifted dependency hash fails
    it with exit 3."""
    repo = _repo(tmp_path)
    helper = _helper(repo, "v1\n")
    entry = {
        "path": "tb/a.spice",
        "cold_start": {"argv": _OK},
        "depends_on": ["models/helpers.inc"],
    }
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    committed.write_text(
        render_envelope(build_attestation(9, _inv(repo, testbenches=[entry])))
    )
    helper.write_text("v2 -- a behaviour-changing edit\n")

    code = main(
        ["signoff", "--attest", "9", "--inventory", _inv(repo, testbenches=[entry])]
        + ["--check", str(committed), "--format", "json"]
    )

    assert code == 3


def test_a_missing_declared_dependency_fails_its_present_check(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "depends_on": ["models/absent.inc"],
                }
            ],
        ),
    )

    (check,) = _dep_checks(envelope)
    assert check["passed"] is False
    assert check["content_hash"] is None
    assert check["testbench"] == "tb/a.spice"
    assert envelope["status"] == "fail"


def test_two_benches_may_declare_the_same_dependency(tmp_path):
    repo = _repo(tmp_path)
    helper = _helper(repo)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "depends_on": ["models/helpers.inc"],
                },
                {
                    "path": "tb/b.spice",
                    "cold_start": {"argv": _OK},
                    "depends_on": ["models/helpers.inc"],
                },
            ],
        ),
    )

    assert envelope["status"] == "pass"
    assert [(c["testbench"], c["content_hash"]) for c in _dep_checks(envelope)] == [
        ("tb/a.spice", _hash(helper)),
        ("tb/b.spice", _hash(helper)),
    ]


def test_a_dependency_may_also_be_an_inventoried_member(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": _OK},
                    "depends_on": ["tb/b.spice"],
                },
                "tb/b.spice",
            ],
            cold_start={"argv": _OK},
        ),
    )

    assert envelope["status"] == "pass"
    (dep,) = _dep_checks(envelope)
    assert dep["path"] == "tb/b.spice"
    assert dep["content_hash"] == _hash(repo / "tb" / "b.spice")


def test_a_duplicate_dependency_within_one_entry_is_refused(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="more than once"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": _OK},
                        "depends_on": ["tb/b.spice", "tb/b.spice"],
                    }
                ],
            ),
        )


@pytest.mark.parametrize(
    "depends_on",
    [
        "tb/b.spice",
        ["/etc/passwd"],
        ["../outside.inc"],
        [""],
        [17],
        [{"path": "tb/b.spice"}],
    ],
)
def test_malformed_depends_on_is_refused(tmp_path, depends_on):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="depends_on"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": _OK},
                        "depends_on": depends_on,
                    }
                ],
            ),
        )


def test_a_dependency_through_a_symlink_is_refused(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.inc"
    outside.write_text(".model x\n")
    (repo / "tb" / "linked.inc").symlink_to(outside)
    with pytest.raises(AttestationError, match="symlink"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": _OK},
                        "depends_on": ["tb/linked.inc"],
                    }
                ],
            ),
        )


def test_an_empty_depends_on_is_accepted(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {"path": "tb/a.spice", "cold_start": {"argv": _OK}, "depends_on": []}
            ],
        ),
    )

    assert envelope["status"] == "pass"
    assert _dep_checks(envelope) == []
    assert [c["declared"] for c in _binding_checks(envelope)] == [0]


def test_a_depends_on_free_inventory_keeps_its_checks(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(repo, testbenches=["tb/b.spice", "tb/a.spice"], cold_start={"argv": _OK}),
    )

    # Apart from the additive dependency_binding rows, the checks are
    # exactly the pre-#5850 ones, in the pre-#5850 order.
    assert [
        (c["check"], c["role"], c.get("path"))
        for c in envelope["attestation"]["checks"]
    ] == [
        ("present", "testbench", "tb/a.spice"),
        ("present", "testbench", "tb/b.spice"),
        ("command", "cold_start", None),
        ("dependency_binding", "testbench", None),
        ("dependency_binding", "testbench", None),
        ("pin", "pdk", None),
        ("pin", "tools", None),
    ]
    assert set(envelope["attestation"]["checks"][0]) == {
        "check",
        "role",
        "path",
        "passed",
        "content_hash",
    }


def test_top_level_depends_on_is_refused(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="unknown field"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=["tb/a.spice"],
                cold_start={"argv": _OK},
                depends_on=["tb/b.spice"],
            ),
        )


def test_the_unknown_entry_field_error_names_depends_on(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(
        AttestationError, match=r"accepted: cold_start, depends_on, path"
    ):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    {"path": "tb/a.spice", "cold_start": {"argv": _OK}, "x": 1}
                ],
            ),
        )


def test_skip_local_only_still_hashes_dependencies(tmp_path, capsys):
    repo = _repo(tmp_path)
    helper = _helper(repo, "v1\n")
    out = repo / "evidence" / "item9.json"
    out.parent.mkdir()
    inventory = _inv(
        repo,
        testbenches=[
            {
                "path": "tb/a.spice",
                "cold_start": {"argv": _OK, "local_only": True},
                "depends_on": ["models/helpers.inc"],
            }
        ],
    )
    capsys.readouterr()
    assert (
        main(
            ["signoff", "--attest", "9", "--inventory", inventory, "-o", str(out)]
            + ["--format", "json"]
        )
        == 0
    )
    helper.write_text("v2 -- refreshed on a host that cannot run the bench\n")
    capsys.readouterr()

    # The local_only command's outcome is carried from OUT; the dependency
    # is a plain file check and must be re-hashed from the new bytes.
    assert (
        main(
            ["signoff", "--attest", "9", "--inventory", inventory, "-o", str(out)]
            + ["--skip-local-only", "--format", "json"]
        )
        == 0
    )
    envelope = json.loads(out.read_text())
    (dep,) = _dep_checks(envelope)
    assert dep["content_hash"] == _hash(helper)
