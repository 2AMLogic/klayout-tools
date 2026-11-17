"""``klt signoff --attest 9``: testbench dependencies declared via
``depends_on`` (issue #5850) -- hashing, drift, refusal and binding
behavior.

Shared fixtures (``make_repo``/``make_inventory``/``OK_ARGV``) live in
``tests/helpers/signoff_item9.py`` alongside the sibling cold-start
suite they were split from.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from helpers.signoff_item9 import OK_ARGV, make_inventory, make_repo
from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    render_envelope,
)

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
    repo = make_repo(tmp_path)
    helper = _helper(repo)
    envelope = build_attestation(
        9,
        make_inventory(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": OK_ARGV},
                    "depends_on": ["models/helpers.inc"],
                },
                "tb/b.spice",
            ],
            cold_start={"argv": OK_ARGV},
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
    repo = make_repo(tmp_path)
    helper = _helper(repo, "v1\n")
    entry = {
        "path": "tb/a.spice",
        "cold_start": {"argv": OK_ARGV},
        "depends_on": ["models/helpers.inc"],
    }
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    committed.write_text(
        render_envelope(build_attestation(9, make_inventory(repo, testbenches=[entry])))
    )
    helper.write_text("v2 -- a behaviour-changing edit\n")

    code = main(
        [
            "signoff",
            "--attest",
            "9",
            "--inventory",
            make_inventory(repo, testbenches=[entry]),
        ]
        + ["--check", str(committed), "--format", "json"]
    )

    assert code == 3


def test_a_missing_declared_dependency_fails_its_present_check(tmp_path):
    repo = make_repo(tmp_path)
    envelope = build_attestation(
        9,
        make_inventory(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": OK_ARGV},
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
    repo = make_repo(tmp_path)
    helper = _helper(repo)
    envelope = build_attestation(
        9,
        make_inventory(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": OK_ARGV},
                    "depends_on": ["models/helpers.inc"],
                },
                {
                    "path": "tb/b.spice",
                    "cold_start": {"argv": OK_ARGV},
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
    repo = make_repo(tmp_path)
    envelope = build_attestation(
        9,
        make_inventory(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": OK_ARGV},
                    "depends_on": ["tb/b.spice"],
                },
                "tb/b.spice",
            ],
            cold_start={"argv": OK_ARGV},
        ),
    )

    assert envelope["status"] == "pass"
    (dep,) = _dep_checks(envelope)
    assert dep["path"] == "tb/b.spice"
    assert dep["content_hash"] == _hash(repo / "tb" / "b.spice")


def test_a_duplicate_dependency_within_one_entry_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(AttestationError, match="more than once"):
        build_attestation(
            9,
            make_inventory(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": OK_ARGV},
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
    repo = make_repo(tmp_path)
    with pytest.raises(AttestationError, match="depends_on"):
        build_attestation(
            9,
            make_inventory(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": OK_ARGV},
                        "depends_on": depends_on,
                    }
                ],
            ),
        )


def test_a_dependency_through_a_symlink_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.inc"
    outside.write_text(".model x\n")
    (repo / "tb" / "linked.inc").symlink_to(outside)
    with pytest.raises(AttestationError, match="symlink"):
        build_attestation(
            9,
            make_inventory(
                repo,
                testbenches=[
                    {
                        "path": "tb/a.spice",
                        "cold_start": {"argv": OK_ARGV},
                        "depends_on": ["tb/linked.inc"],
                    }
                ],
            ),
        )


def test_an_empty_depends_on_is_accepted(tmp_path):
    repo = make_repo(tmp_path)
    envelope = build_attestation(
        9,
        make_inventory(
            repo,
            testbenches=[
                {
                    "path": "tb/a.spice",
                    "cold_start": {"argv": OK_ARGV},
                    "depends_on": [],
                }
            ],
        ),
    )

    assert envelope["status"] == "pass"
    assert _dep_checks(envelope) == []
    assert [c["declared"] for c in _binding_checks(envelope)] == [0]


def test_a_depends_on_free_inventory_keeps_its_checks(tmp_path):
    repo = make_repo(tmp_path)
    envelope = build_attestation(
        9,
        make_inventory(
            repo, testbenches=["tb/b.spice", "tb/a.spice"], cold_start={"argv": OK_ARGV}
        ),
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
    repo = make_repo(tmp_path)
    with pytest.raises(AttestationError, match="unknown field"):
        build_attestation(
            9,
            make_inventory(
                repo,
                testbenches=["tb/a.spice"],
                cold_start={"argv": OK_ARGV},
                depends_on=["tb/b.spice"],
            ),
        )


def test_the_unknown_entry_field_error_names_depends_on(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(
        AttestationError, match=r"accepted: cold_start, depends_on, path"
    ):
        build_attestation(
            9,
            make_inventory(
                repo,
                testbenches=[
                    {"path": "tb/a.spice", "cold_start": {"argv": OK_ARGV}, "x": 1}
                ],
            ),
        )


def test_skip_local_only_still_hashes_dependencies(tmp_path, capsys):
    repo = make_repo(tmp_path)
    helper = _helper(repo, "v1\n")
    out = repo / "evidence" / "item9.json"
    out.parent.mkdir()
    inventory = make_inventory(
        repo,
        testbenches=[
            {
                "path": "tb/a.spice",
                "cold_start": {"argv": OK_ARGV, "local_only": True},
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
