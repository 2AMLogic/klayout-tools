"""Digest, load, and pin contracts for local OSDI verification."""

import hashlib
import json
import sys

import pytest

from klayout_tools.cli import main


def install(tmp_path, names=("one.osdi", "two.osdi")):
    variant = tmp_path / "install" / "ihp-sg13g2"
    ngspice = variant / "libs.tech/ngspice"
    directory = ngspice / "osdi"
    directory.mkdir(parents=True)
    lines = ["* osdi 'old.osdi'", "set unrelated = $(touch NEVER)"]
    for name in names:
        lines.append(f"osdi '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/{name}'")
        (directory / name).write_text(name)
    (ngspice / ".spiceinit").write_text("\n".join(lines))
    return variant


def check(root, capsys, *options, variant="ihp-sg13g2"):
    status = main(
        [
            "pdk",
            "osdi",
            "--pdk",
            variant,
            "--pdk-root",
            str(root),
            "--format",
            "json",
            *options,
        ]
    )
    captured = capsys.readouterr()
    data = json.loads(captured.err if status else captured.out)
    assert str(root.parent) not in json.dumps(data)
    return status, data


@pytest.mark.parametrize("flat", [True, False])
def test_declarations_in_order_no_manifest_or_ngspice(
    tmp_path, monkeypatch, capsys, flat
):
    names = ["two.osdi", "one.osdi", "new_cap.osdi"]
    variant = install(tmp_path, names)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    status, report = check(variant if flat else variant.parent, capsys)
    assert status == 0 and report["load_probe"] is None
    assert not (variant / "libs.tech/ngspice/osdi/.build-manifest").exists()
    rows = report["identity"]["models"]
    assert [r["name"] for r in rows] == names
    for item in rows:
        assert item["present"] and item["scope"] == "external" and item["path"] is None
        assert item["sha256"] == hashlib.sha256(item["name"].encode()).hexdigest()


@pytest.mark.parametrize("whole", [False, True])
def test_missing_model_is_named_without_digest(tmp_path, capsys, whole):
    variant = install(tmp_path)
    directory = variant / "libs.tech/ngspice/osdi"
    (directory / "one.osdi").unlink()
    if whole:
        (directory / "two.osdi").unlink()
        directory.rmdir()
    status, result = check(variant, capsys)
    error = result["error"]
    assert status and error["code"] == "osdi_model_missing"
    assert error["identity"]["models"][0] == {
        "name": "one.osdi",
        "present": False,
        "sha256": None,
        "scope": "external",
        "path": None,
    }


@pytest.mark.parametrize("change", ["digest", "missing_name", "extra_name"])
def test_exact_expectation(tmp_path, capsys, change):
    variant = install(tmp_path)
    pins = {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in ("one.osdi", "two.osdi")
    }
    expect = tmp_path / "pins.json"
    expect.write_text(json.dumps(pins))
    assert check(variant, capsys, "--expect", str(expect))[0] == 0
    if change == "digest":
        pins["one.osdi"] = "0" * 64
    elif change == "extra_name":
        pins["extra.osdi"] = "0" * 64
    else:
        del pins["one.osdi"]
    expect.write_text(json.dumps(pins))
    status, result = check(variant, capsys, "--expect", str(expect))
    assert status and result["error"]["code"] == "osdi_pin_mismatch"


def test_unknown_setup_and_pdk_without_osdi(tmp_path, capsys):
    variant = install(tmp_path)
    (variant / "libs.tech/ngspice/.spiceinit").unlink()
    assert check(variant, capsys)[1]["error"]["code"] == "osdi_setup_unreadable"
    sky = tmp_path / "sky130A"
    (sky / "libs.tech/klayout").mkdir(parents=True)
    status, result = check(sky, capsys, "--load-probe", variant="sky130A")
    assert status == 0 and result["identity"]["models"] == []


@pytest.mark.parametrize("mode", ["ok", "reject", "no_registration", "exit"])
def test_probe_requires_positive_isolated_model_registration(
    tmp_path, capsys, monkeypatch, mode
):
    variant = install(tmp_path)
    binary = tmp_path / "ngspice"
    binary.write_text(f"""#!{sys.executable}
import os,sys
from pathlib import Path
if '--version' in sys.argv:
 print('ngspice-46 test build'); sys.exit(0)
assert '-n' in sys.argv and '-b' in sys.argv
assert 'devhelp' in Path(sys.argv[-1]).read_text()
print('default : A simulator independent device loaded with OSDI')
if 'pre_osdi' not in Path(sys.argv[-1]).read_text(): sys.exit(0)
model=Path('model.osdi').read_bytes().decode()
if model=='two.osdi' and {mode!r}=='reject':
 print("Error: Library couldn't be loaded!"); sys.exit(0)
if model=='two.osdi' and {mode!r}=='exit': sys.exit(2)
if model!='two.osdi' or {mode!r}!='no_registration':
 print(model.split('.')[0]+' : A simulator independent device loaded with OSDI')
""")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    status, result = check(variant, capsys, "--load-probe")
    if mode != "ok":
        assert status and result["error"]["code"] == "osdi_load_failed"
        result = result["error"]
    else:
        assert status == 0
        assert (
            main(
                [
                    "pdk",
                    "osdi",
                    "--pdk",
                    "ihp-sg13g2",
                    "--pdk-root",
                    str(variant),
                    "--load-probe",
                    "--format",
                    "text",
                ]
            )
            == 0
        )
        assert (
            capsys.readouterr().out.splitlines()[-1]
            == "Verified registration on ngspice 46"
        )
    probe = result["load_probe"]
    assert (
        probe["engine_build"]["binary_sha256"]
        == hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    assert probe["engine_version"] == "46"
    assert probe["models"][0]["devices"] == ["one"]
    assert probe["models"][1]["loaded"] == (mode == "ok")


def test_missing_executable_is_attributed_to_the_load_probe(
    tmp_path, monkeypatch, capsys
):
    variant = install(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    status, result = check(variant, capsys, "--load-probe")
    assert status and result["error"]["code"] == "osdi_load_failed"
    rows = result["error"]["load_probe"]["models"]
    assert [row["name"] for row in rows] == ["one.osdi", "two.osdi"]
    assert all(
        not row["loaded"] and row["reason"] == "ngspice_unavailable" for row in rows
    )


def test_opaque_setup_and_malformed_pins_do_not_disclose_host_paths(tmp_path, capsys):
    variant = install(tmp_path)
    pins = tmp_path / "pins"
    pins.write_text(json.dumps({str(tmp_path / "disclose.osdi"): "0" * 64}))
    assert (
        check(variant, capsys, "--expect", str(pins))[1]["error"]["code"]
        == "osdi_invalid_expectation"
    )
    setup = variant / "libs.tech/ngspice/.spiceinit"
    setup.write_text("osdi '$SOME_OTHER_DIR/model.osdi'\n")
    assert check(variant, capsys)[1]["error"]["code"] == "osdi_setup_unreadable"


def test_text_inventory_lists_each_model_digest(tmp_path, capsys):
    names = ["two.osdi", "one.osdi"]
    variant = install(tmp_path, names)
    assert (
        main(
            [
                "pdk",
                "osdi",
                "--pdk",
                "ihp-sg13g2",
                "--pdk-root",
                str(variant),
                "--format",
                "text",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [
        "ihp-sg13g2 OSDI models: 2",
        *(f"{name}  {hashlib.sha256(name.encode()).hexdigest()}" for name in names),
    ]


def test_unresolved_pdk_reports_a_path_free_cli_error(tmp_path, capsys):
    code, report = check(tmp_path / "absent", capsys)
    assert code == 1
    assert report["error"]["code"] == "pdk_not_found"
