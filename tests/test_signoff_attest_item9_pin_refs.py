"""``klt signoff --attest 9``: reference-form pins, a pin declared by the
file that owns its value (issue #5563)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    render_envelope,
)
from test_signoff_attest_item9 import _OK, _PINS, _inv, _repo

_TOOLCHAIN = {"tools": {"ngspice": {"commit": "a1b2c3d4e5f6"}}}
_NG_REF = {"from": "config/toolchain.json", "key": "tools.ngspice.commit"}


def _pin_repo(tmp_path, pins, files) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return (
        repo,
        _inv(repo, pins=pins, testbenches=["tb/a.spice"], cold_start={"argv": _OK}),
    )


def _toolchain_repo(tmp_path, pins, toolchain=_TOOLCHAIN) -> tuple[Path, str]:
    return _pin_repo(tmp_path, pins, {"config/toolchain.json": json.dumps(toolchain)})


def _pin_check(envelope: dict, role: str) -> dict:
    return next(
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "pin" and c["role"] == role
    )


def test_reference_pin_resolves_and_records_where_it_came_from(tmp_path):
    _, inventory = _toolchain_repo(
        tmp_path,
        {
            "pdk": {"name": "sky130A", "revision": "0fe599b2"},
            "tools": {"ngspice": _NG_REF},
        },
    )

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "pass"
    tools = _pin_check(envelope, "tools")
    assert tools["passed"] is True
    assert tools["value"] == {"ngspice": "a1b2c3d4e5f6"}
    assert tools["resolved_from"] == {"ngspice": _NG_REF}


def test_pdk_name_and_revision_accept_the_reference_form(tmp_path):
    _, inventory = _pin_repo(
        tmp_path,
        {
            "pdk": {
                "name": {"from": "config/pdk.json", "key": "pdk.name"},
                "revision": {"from": "config/pdk.json", "key": "pdk.revision"},
            },
            "tools": {"ngspice": "42"},
        },
        {
            "config/pdk.json": json.dumps(
                {"pdk": {"name": "sky130A", "revision": "0fe599b2"}}
            )
        },
    )

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "pass"
    pdk = _pin_check(envelope, "pdk")
    assert pdk["passed"] is True
    assert pdk["value"] == {"name": "sky130A", "revision": "0fe599b2"}
    assert pdk["resolved_from"] == {
        "name": {"from": "config/pdk.json", "key": "pdk.name"},
        "revision": {"from": "config/pdk.json", "key": "pdk.revision"},
    }
    assert "resolved_from" not in _pin_check(envelope, "tools")


def test_no_pdk_form_is_not_treated_as_a_reference(tmp_path):
    _, inventory = _toolchain_repo(
        tmp_path,
        {
            "pdk": {"none": True, "reason": "behavioral RTL only"},
            "tools": {"ngspice": _NG_REF},
        },
    )

    envelope = build_attestation(9, inventory)

    assert _pin_check(envelope, "pdk") == {
        "check": "pin",
        "role": "pdk",
        "passed": True,
        "value": {"none": True, "reason": "behavioral RTL only"},
    }
    assert "resolved_from" in _pin_check(envelope, "tools")


def test_tools_may_mix_a_literal_and_a_reference(tmp_path):
    _, inventory = _toolchain_repo(
        tmp_path, {"tools": {"klt": "0.7.0", "ngspice": _NG_REF}}
    )

    envelope = build_attestation(9, inventory)

    tools = _pin_check(envelope, "tools")
    assert tools["passed"] is True
    assert tools["value"] == {"klt": "0.7.0", "ngspice": "a1b2c3d4e5f6"}
    assert tools["resolved_from"] == {"ngspice": _NG_REF}


@pytest.mark.parametrize(
    "ref, toolchain",
    [
        ({"from": "config/absent.json", "key": "x"}, _TOOLCHAIN),
        (_NG_REF, {"tools": {"ngspice": {}}}),
        (_NG_REF, {"tools": {"ngspice": {"commit": 42}}}),
        (_NG_REF, {"tools": {"ngspice": {"commit": ""}}}),
        (_NG_REF, {"tools": {"ngspice": {"commit": {"deep": 1}}}}),
        ({"from": "config/toolchain.json", "key": "tools"}, _TOOLCHAIN),
        ({"from": "config/toolchain.json", "key": "  "}, _TOOLCHAIN),
        ({"from": "config/toolchain.json", "key": 5}, _TOOLCHAIN),
        ({"from": "config/toolchain.json"}, _TOOLCHAIN),
        ({"from": "config/toolchain.json", "key": "x", "extra": 1}, _TOOLCHAIN),
        (_NG_REF, "not json {"),
    ],
)
def test_unresolvable_reference_fails_the_check_without_raising(
    tmp_path, ref, toolchain
):
    _, inventory = _toolchain_repo(tmp_path, {"tools": {"ngspice": ref}}, toolchain)

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "fail"
    assert _pin_check(envelope, "tools")["passed"] is False


@pytest.mark.parametrize(
    "from_value, message",
    [
        ("/etc/hosts", "absolute"),
        ("../toolchain.json", "outside the repository"),
        (5, "non-empty path strings"),
    ],
)
def test_escaping_from_is_refused(tmp_path, from_value, message):
    _, inventory = _toolchain_repo(
        tmp_path, {"tools": {"ngspice": {"from": from_value, "key": "x"}}}
    )

    with pytest.raises(AttestationError, match=message):
        build_attestation(9, inventory)


def test_symlinked_from_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "config").mkdir()
    (repo / "config" / "toolchain.json").write_text(json.dumps(_TOOLCHAIN))
    (repo / "config" / "link.json").symlink_to("toolchain.json")
    inventory = _inv(
        repo,
        pins={"tools": {"ngspice": {"from": "config/link.json", "key": "x"}}},
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK},
    )

    with pytest.raises(AttestationError, match="symlink"):
        build_attestation(9, inventory)


def test_literal_only_pins_keep_the_envelope_unchanged(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK}
    )  # the default _PINS fixture: literal-only

    envelope = build_attestation(9, inventory)

    assert _pin_check(envelope, "pdk") == {
        "check": "pin",
        "role": "pdk",
        "passed": True,
        "value": _PINS["pdk"],
    }
    assert _pin_check(envelope, "tools") == {
        "check": "pin",
        "role": "tools",
        "passed": True,
        "value": {"ngspice": "42"},
    }
    assert "resolved_from" not in render_envelope(envelope)
    # And a committed literal envelope re-checks clean (no re-attest churn).
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    committed.write_text(render_envelope(envelope))
    assert (
        main(
            ["signoff", "--attest", "9", "--inventory", inventory]
            + ["--check", str(committed), "--format", "json"]
        )
        == 0
    )


def test_check_drifts_after_the_referenced_value_changes(tmp_path, capsys):
    repo, inventory = _toolchain_repo(tmp_path, {"tools": {"ngspice": _NG_REF}})
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    committed.write_text(render_envelope(build_attestation(9, inventory)))
    (repo / "config" / "toolchain.json").write_text(
        json.dumps({"tools": {"ngspice": {"commit": "bumped"}}})
    )
    capsys.readouterr()

    code = main(
        ["signoff", "--attest", "9", "--inventory", inventory]
        + ["--check", str(committed), "--format", "json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 3
    assert report["status"] == "drifted"
    assert any(entry["field"].endswith("value.ngspice") for entry in report["drift"])
