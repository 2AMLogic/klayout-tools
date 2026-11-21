"""``klt pdk find|check --expect-version`` (issue #3810). Hermetic tmp trees."""

import json

import pytest

from klayout_tools.cli import main

SHA = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
OTHER = "c6d73a35f524070e85faff4a6a9eef49553ebc2b"


def _install(root, sources=None, marker=None, broken=False):
    variant_dir = root / "sky130A"
    (variant_dir / "libs.tech" / "ngspice").mkdir(parents=True)
    if sources is not None:
        (variant_dir / "SOURCES").write_text(sources, encoding="utf-8")
    if marker is not None:
        (variant_dir / ".fetched-version").write_text(marker, encoding="utf-8")
    if broken:
        (variant_dir / "libs.tech" / "ngspice" / "dangling").symlink_to(
            "/nonexistent/x"
        )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _run(capsys, tmp_path, verb, *extra):
    argv = [
        "pdk",
        verb,
        "--pdk",
        "sky130A",
        "--pdk-root",
        str(tmp_path / "r"),
        "--format",
        "json",
        *extra,
    ]
    code = main(argv)
    out, err = capsys.readouterr()
    return code, json.loads(out), err


@pytest.mark.parametrize("verb", ["find", "check"])
@pytest.mark.parametrize("expect", [SHA, SHA[:7], f"open_pdks {SHA}"])
def test_match(capsys, tmp_path, verb, expect):
    _install(tmp_path / "r", f"open_pdks {SHA}\n")
    code, report, err = _run(capsys, tmp_path, verb, "--expect-version", expect)
    assert code == 0 and err == ""
    assert report["version_expectation"]["status"] == "match"


@pytest.mark.parametrize("verb", ["find", "check"])
def test_mismatch_exits_6_naming_both(capsys, tmp_path, verb):
    _install(tmp_path / "r", f"open_pdks {SHA}\n")
    code, report, err = _run(capsys, tmp_path, verb, "--expect-version", OTHER)
    assert code == 6
    assert OTHER in err and SHA in err
    assert report["version_expectation"] == {
        "status": "mismatch",
        "expected": OTHER,
        "installed": f"open_pdks {SHA}",
    }


def test_no_sources_is_unknown_exit_6(capsys, tmp_path):
    _install(tmp_path / "r")
    code, report, err = _run(capsys, tmp_path, "find", "--expect-version", SHA)
    assert code == 6 and SHA in err
    assert report["version_expectation"]["status"] == "unknown"
    assert report["version_expectation"]["installed"] is None


def test_lambdapdk_tree_vs_sha_is_unknown(capsys, tmp_path):
    _install(tmp_path / "r", marker="0.2.17\n")
    code, report, _ = _run(capsys, tmp_path, "find", "--expect-version", SHA)
    assert code == 6
    assert report["version_expectation"]["status"] == "unknown"
    assert report["version_expectation"]["installed"] == "0.2.17"


def test_lambdapdk_exact_string_match(capsys, tmp_path):
    _install(tmp_path / "r", marker="0.2.17\n")
    code, report, _ = _run(capsys, tmp_path, "find", "--expect-version", "0.2.17")
    assert code == 0
    assert report["version_expectation"]["status"] == "match"


def test_flag_absent_changes_nothing(capsys, tmp_path):
    _install(tmp_path / "r", f"open_pdks {OTHER}\n")
    code, report, _ = _run(capsys, tmp_path, "find")
    assert code == 0 and "version_expectation" not in report


def test_broken_symlink_wins_over_mismatch(capsys, tmp_path):
    _install(tmp_path / "r", f"open_pdks {SHA}\n", broken=True)
    code, report, err = _run(capsys, tmp_path, "check", "--expect-version", OTHER)
    assert code == 4
    assert report["version_expectation"]["status"] == "mismatch"
    assert OTHER in err
