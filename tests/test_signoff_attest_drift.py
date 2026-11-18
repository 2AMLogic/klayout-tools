"""``klt signoff --attest --check`` names the drifted check, not only its
list index (issue #5460).

The tests reuse the item-9 fixtures (its inventory is the richest check
surface to drift against); the helpers live in
``tests/helpers/signoff_item9``."""

from __future__ import annotations

import sys
from pathlib import Path

from helpers.signoff_item9 import (
    OK_ARGV as _OK,
)
from helpers.signoff_item9 import (
    PINS as _PINS,
)
from helpers.signoff_item9 import (
    make_inventory as _inv,
)
from helpers.signoff_item9 import (
    make_repo as _repo,
)
from klayout_tools.signoff_attest import (
    build_attestation,
    check_attestation,
    render_envelope,
)

# --------------------------------------------------------------------------- #
# `--attest --check` drift names the check, not only its list index
# (issue #5460)
# --------------------------------------------------------------------------- #


def _attested(repo: Path, inventory: str) -> str:
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir(exist_ok=True)
    committed.write_text(render_envelope(build_attestation(9, inventory)))
    return str(committed)


def test_check_drift_names_the_drifted_testbench_path(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo, testbenches=["tb/a.spice", "tb/b.spice"], cold_start={"argv": _OK}
    )
    committed = _attested(repo, inventory)
    (repo / "tb" / "a.spice").write_text("* a, edited\n")

    result = check_attestation(9, inventory, committed)

    (hashed,) = [
        e
        for e in result["drift"]
        if e["field"].startswith("attestation.checks.")
        and e["field"].endswith(".content_hash")
    ]
    assert hashed["field"] == "attestation.checks.0.content_hash"
    assert hashed["check"] == {"role": "testbench", "path": "tb/a.spice"}


def test_check_drift_names_a_drifted_command_by_its_testbench(tmp_path):
    """A command check whose *outcome* moved while its argv did not: the
    identity has to come from the check, there is no path in the pointer."""
    repo = _repo(tmp_path)
    flag = tmp_path / "flag"
    flag.write_text("present\n")
    probe = [
        sys.executable,
        "-c",
        f"import os, sys; sys.exit(0 if os.path.exists({str(flag)!r}) else 1)",
    ]
    inventory = _inv(
        repo, testbenches=[{"path": "tb/a.spice", "cold_start": {"argv": probe}}]
    )
    committed = _attested(repo, inventory)
    flag.unlink()

    result = check_attestation(9, inventory, committed)

    moved = [
        e
        for e in result["drift"]
        if e["field"].endswith((".exit_status", ".passed"))
        and e["field"].startswith("attestation.checks.")
    ]
    assert moved, result["drift"]
    for entry in moved:
        assert entry["check"] == {"role": "cold_start", "testbench": "tb/a.spice"}


def test_pins_are_identified_by_role_alone(tmp_path):
    """The `pdk`/`tools` pins carry no identity key beyond `role`, so their
    `check` object is `{"role": ...}` -- not an absent key."""
    repo = _repo(tmp_path)
    good = _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK})
    committed = _attested(repo, good)
    moved = _inv(
        repo,
        pins={**_PINS, "tools": {"ngspice": "43"}},
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK},
    )

    result = check_attestation(9, moved, committed)

    pin_drift = [e for e in result["drift"] if e.get("check") == {"role": "tools"}]
    assert pin_drift, result["drift"]
    assert all(e["field"].startswith("attestation.checks.") for e in pin_drift)


def test_an_added_testbench_still_reports_one_unannotated_whole_list_entry(tmp_path):
    """The length-mismatch branch of the diff names no single check, so it
    gets no `check` key (identity-based matching is out of scope, #5460)."""
    repo = _repo(tmp_path)
    committed = _attested(
        repo, _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK})
    )
    grown = _inv(
        repo, testbenches=["tb/a.spice", "tb/b.spice"], cold_start={"argv": _OK}
    )

    result = check_attestation(9, grown, committed)

    whole_list = [e for e in result["drift"] if e["field"] == "attestation.checks"]
    assert len(whole_list) == 1
    assert "check" not in whole_list[0]
    assert not any(
        e["field"].startswith("attestation.checks.") for e in result["drift"]
    )


def test_a_top_level_cold_start_is_identified_by_role_alone(tmp_path):
    """A top-level `cold_start` is one check covering every bench, so it has
    no `testbench` to name: `{"role": "cold_start"}` is its whole identity."""
    repo = _repo(tmp_path)
    flag = tmp_path / "flag"
    flag.write_text("present\n")
    probe = [
        sys.executable,
        "-c",
        f"import os, sys; sys.exit(0 if os.path.exists({str(flag)!r}) else 1)",
    ]
    inventory = _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": probe})
    committed = _attested(repo, inventory)
    flag.unlink()

    result = check_attestation(9, inventory, committed)

    moved = [e for e in result["drift"] if e["field"].endswith(".exit_status")]
    assert [e["check"] for e in moved] == [{"role": "cold_start"}]
