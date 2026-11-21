"""Native gf180mcu 3LM metal-slot execution and prelude identity (#6916)."""

import hashlib
import json
from pathlib import Path

import pytest

from klayout_tools import _drc_gf180_3lm
from klayout_tools.cli import main
from test_drc_gf180mcu_moscap import _SKIP_NO_NATIVE_TIER
from test_drc_gf180mcu_native_deck import _install
from test_drc_klayout_engine import (
    _EDGE_PAIR_RDB,
    _EMPTY_RDB,
    _stub_by_input,
    _stub_klayout_drc_subprocess,
    _write_gds,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.parametrize(
    ("variant", "overrides", "explicit_file", "expect_prelude"),
    [
        ("gf180mcuA", [], False, True),
        ("gf180mcuB", [], False, False),
        ("gf180mcuA", ["metal_level=4LM"], False, False),
        ("gf180mcuD", ["metal_level=3LM"], False, True),
        ("gf180mcuD", ["variant=A"], False, True),
        ("gf180mcuA", ["variant=gf180mcuC"], False, False),
        ("gf180mcuA", ["metal_level=3LM"], True, False),
    ],
)
def test_family_stack_controls_macro_and_provenance(
    tmp_path, monkeypatch, capsys, variant, overrides, explicit_file, expect_prelude
):
    root = _install(tmp_path, variant, ("gf180mcu.drc",))
    deck = Path(root, variant, "libs.tech/klayout/tech/drc/gf180mcu.drc")
    layout = _write_gds(tmp_path / "chip.gds")
    cmds = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=cmds)
    args = ["drc", layout, "--engine", "klayout", "--pdk", variant, "--pdk-root", root]
    if explicit_file:
        args.extend(["--deck-file", str(deck)])
    for value in overrides:
        args.extend(["--deck-var", value])
    code = main([*args, "--format", "json"])
    capture = capsys.readouterr()
    assert code == 4, capture.err
    report = json.loads(capture.out)
    assert report["status"] == "coverage_unknown"
    (cmd,) = cmds
    if expect_prelude:
        assert cmd[1:3] == ["-b", "-rm"]
        path = Path(cmd[3])
        assert cmd[4:6] == ["-r", str(deck)]
        assert path.name == "gf180mcu_3lm_prelude.rb"
        (entry,) = report["native_deck"]["preludes"]
        assert entry["name"] == path.name
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert "metal4_drawn" in entry["reason"]
        prov = report["provenance"]["deck"]["preludes"][0]
        assert (prov["name"], prov["sha256"]) == (entry["name"], entry["sha256"])
    else:
        assert cmd[1:4] == ["-b", "-r", str(deck)]
        assert "-rm" not in cmd
        assert "preludes" not in report.get("native_deck", {})
        assert "preludes" not in report["provenance"]["deck"]


@_SKIP_NO_NATIVE_TIER
def test_unslotted_metal1_trips_foundry_rule(tmp_path, capsys):
    layout = _write_gds(tmp_path / "wide.gds", layer=(34, 0), box=(0, 0, 40000, 40000))
    args = ["drc", layout, "--engine", "klayout", "--pdk", "gf180mcuA"]
    code = main([*args, "--deck-var", "decks=mslot", "--format", "json"])
    capture = capsys.readouterr()
    assert code == 3, capture.err
    report = json.loads(capture.out)
    assert report["status"] == "violations"
    assert report["rule_counts"]["MSLOT1.1"] >= 1
    categories = report["coverage"]["rule_categories"]
    for level in (1, 2, 3):
        assert f"MSLOT{level}.1" in categories
        assert f"Checking slots for Metal{level}" in report["deck_output"]["stdout"]
    assert report["coverage"]["known"] is False


def test_saved_slot_report_replays_and_detects_changed_prelude(
    tmp_path, capsys, monkeypatch
):
    root = _install(tmp_path, "gf180mcuA", ("gf180mcu.drc",))
    _stub_klayout_drc_subprocess(monkeypatch)
    layout = _write_gds(tmp_path / "chip.gds")
    args = [
        "drc",
        layout,
        "--engine",
        "klayout",
        "--pdk",
        "gf180mcuA",
        "--pdk-root",
        root,
    ]
    assert main([*args, "--deck-var", "decks=mslot", "--format", "json"]) == 4
    report_file = tmp_path / "native.json"
    report_file.write_text(capsys.readouterr().out)
    check = ["drc", layout, "--check", str(report_file), "--format", "json"]
    assert main(check) == 0
    entries = json.loads(capsys.readouterr().out)["checks"]
    assert any("preludes" in entry["field"] and entry["match"] for entry in entries)
    assert main([*check, "--rerun"]) == 0
    capsys.readouterr()

    packaged = _drc_gf180_3lm.PRELUDE
    replacement = tmp_path / packaged.name
    replacement.write_bytes(packaged.read_bytes() + b"\n# revised macro\n")
    monkeypatch.setattr(_drc_gf180_3lm, "PRELUDE", replacement)
    assert main(check) == 3
    entries = json.loads(capsys.readouterr().out)["checks"]
    assert any("preludes" in entry["field"] and not entry["match"] for entry in entries)


@_SKIP_NO_NATIVE_TIER
def test_zero_findings_stay_unknown_after_passing_wide_metal_control(tmp_path, capsys):
    narrow = _write_gds(tmp_path / "narrow.gds", layer=(34, 0))
    wide = _write_gds(tmp_path / "wide.gds", layer=(34, 0), box=(0, 0, 40000, 40000))
    args = ["drc", narrow, "--engine", "klayout", "--pdk", "gf180mcuA"]
    code = main(
        [
            *args,
            "--deck-var",
            "decks=mslot",
            "--negative-control",
            wide,
            "--negative-control-categories",
            "MSLOT1.1",
            "--format",
            "json",
        ]
    )
    capture = capsys.readouterr()
    assert code == 4, capture.err
    report = json.loads(capture.out)
    assert report["status"] == "coverage_unknown"
    assert report["violation_count"] == 0
    assert report["negative_control"]["status"] == "passed"
    assert "MSLOT1.1" in report["negative_control"]["rules_found"]


def test_rerun_preserves_install_with_symlinked_decks(tmp_path, monkeypatch, capsys):
    variant = "gf180mcu-local"
    root = _install(tmp_path, variant, ("gf180mcu.drc",))
    deck_dir = Path(root, variant, "libs.tech/klayout/tech/drc")
    shared = deck_dir.rename(tmp_path / "shared-decks")
    deck_dir.symlink_to(shared, target_is_directory=True)
    # Another install of this variant must never supply the replay provenance.
    other = _install(tmp_path / "other", variant, ("gf180mcu.drc",))
    monkeypatch.setenv("PDK_ROOT", other)
    layout = _write_gds(tmp_path / "chip.gds")
    _stub_klayout_drc_subprocess(monkeypatch)
    args = [
        "drc",
        layout,
        "--engine",
        "klayout",
        "--pdk",
        variant,
        "--pdk-root",
        root,
        "--deck-var",
        "variant=A",
        "--deck-var",
        "decks=mslot",
        "--format",
        "json",
    ]
    assert main(args) == 4
    saved = tmp_path / "native.json"
    saved.write_text(capsys.readouterr().out)
    assert (
        main(["drc", layout, "--check", str(saved), "--rerun", "--format", "json"]) == 0
    )
    replay = json.loads(capsys.readouterr().out)
    assert replay["fresh"]["provenance"]["pdk"]["source"] == "--pdk-root flag"

    # An explicit deck override outside the install can still use its provenance.
    original = json.loads(saved.read_text())
    kwargs = _drc_gf180_3lm.replay_prelude_kwargs(
        original, str(shared / "gf180mcu.drc")
    )
    assert kwargs["pdk_root"] == root


def test_extracted_native_report_replays(tmp_path, monkeypatch, capsys):
    root = _install(tmp_path, "gf180mcuA", ("gf180mcu.drc",))
    layout = _write_gds(tmp_path / "wide.gds", layer=(34, 0), box=(0, 0, 40000, 40000))
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EDGE_PAIR_RDB)
    args = [
        "drc",
        layout,
        "--engine",
        "both",
        "--deck",
        "gf180mcu",
        "--pdk",
        "gf180mcuA",
        "--pdk-root",
        root,
        "--deck-var",
        "decks=mslot",
        "--format",
        "json",
    ]
    assert main(args) == 3
    envelope = json.loads(capsys.readouterr().out)
    native = envelope["reports"]["native"]
    assert native["native_deck"] == envelope["native_deck"]
    saved = tmp_path / "native.json"
    saved.write_text(json.dumps(native))
    assert (
        main(["drc", layout, "--check", str(saved), "--rerun", "--format", "json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "match"


def test_rerun_executes_recorded_negative_control(tmp_path, monkeypatch, capsys):
    root = _install(tmp_path, "gf180mcuA", ("gf180mcu.drc",))
    narrow = _write_gds(tmp_path / "narrow.gds")
    wide = _write_gds(tmp_path / "wide.gds")
    outputs = {"wide.gds": _EDGE_PAIR_RDB, "narrow.gds": _EMPTY_RDB}
    _stub_by_input(monkeypatch, outputs)
    args = [
        "drc",
        narrow,
        "--engine",
        "klayout",
        "--pdk",
        "gf180mcuA",
        "--pdk-root",
        root,
        "--deck-var",
        "decks=mslot",
        "--negative-control",
        wide,
        "--negative-control-categories",
        "W.1",
        "--format",
        "json",
    ]
    assert main(args) == 4
    saved = tmp_path / "native.json"
    saved.write_text(capsys.readouterr().out)
    check = ["drc", narrow, "--check", str(saved), "--rerun", "--format", "json"]
    assert main(check) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["fresh"]["negative_control"]["expected_categories"] == ["W.1"]
    outputs["wide.gds"] = _EDGE_PAIR_RDB.replace("W.1", "OTHER")
    assert main(check) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert "missing expected categories ['W.1']" in error["message"]
