"""``klt pdk find|check --expect-version`` (issue #3810) and
``check --expect-bundle-sha256`` (issue #5624). Hermetic tmp trees."""

import json

import pytest

from klayout_tools.cli import main

SHA = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
OTHER = "c6d73a35f524070e85faff4a6a9eef49553ebc2b"
BUNDLE = "9f2c" * 16  # 64-hex bundle sha256
BUNDLE_OTHER = "0a5b" * 16


def _install(root, sources=None, marker=None, broken=False, store=None):
    variant_dir = root / "sky130A"
    (variant_dir / "libs.tech" / "ngspice").mkdir(parents=True)
    if sources is not None:
        (variant_dir / "SOURCES").write_text(sources, encoding="utf-8")
    if marker is not None:
        (variant_dir / ".fetched-version").write_text(marker, encoding="utf-8")
    if store is not None:
        (variant_dir / ".pdk-store.json").write_text(
            json.dumps(store), encoding="utf-8"
        )
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


# --------------------------------------------------------------------------- #
# Issue #5624: the store identity namespace (`.pdk-store.json`)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("verb", ["find", "check"])
@pytest.mark.parametrize("expect", [OTHER, OTHER[:7]])
def test_store_only_full_and_short_pin_match(capsys, tmp_path, verb, expect):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    code, report, err = _run(capsys, tmp_path, verb, "--expect-version", expect)
    assert code == 0 and err == ""
    assert report["version_expectation"] == {
        "status": "match",
        "expected": expect,
        "installed": OTHER,
    }


def test_store_only_different_sha_is_mismatch_exit_6(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER})
    code, report, err = _run(capsys, tmp_path, "check", "--expect-version", SHA)
    assert code == 6
    assert SHA in err and OTHER in err
    assert report["version_expectation"] == {
        "status": "mismatch",
        "expected": SHA,
        "installed": OTHER,
    }


def test_store_exact_string_pin_matches_in_store_namespace(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": "2026.11.14"})
    code, report, _ = _run(capsys, tmp_path, "find", "--expect-version", "2026.11.14")
    assert code == 0
    assert report["version_expectation"]["status"] == "match"
    assert report["version_expectation"]["installed"] == "2026.11.14"


def test_store_open_pdks_stamp_pin_is_unknown(capsys, tmp_path):
    # An explicit `open_pdks <sha>` stamp names the SOURCES namespace; against
    # a store install the namespaces differ, so the result stays `unknown`.
    _install(tmp_path / "r", store={"version": OTHER})
    code, report, err = _run(
        capsys, tmp_path, "find", "--expect-version", f"open_pdks {SHA}"
    )
    assert code == 6
    assert report["version_expectation"]["status"] == "unknown"
    assert report["version_expectation"]["installed"] == OTHER


def test_store_stamp_at_parent_dir_matches_too(capsys, tmp_path):
    # #5318's documented placement: one level above the (real) variant dir.
    root = tmp_path / "r"
    variant_dir = root / "sky130A"
    (variant_dir / "libs.tech").mkdir(parents=True)
    (root / ".pdk-store.json").write_text(
        json.dumps({"version": OTHER}), encoding="utf-8"
    )
    code, report, _ = _run(capsys, tmp_path, "check", "--expect-version", OTHER[:11])
    assert code == 0
    assert report["version_expectation"]["status"] == "match"


def test_sources_wins_over_store_stamp(capsys, tmp_path):
    _install(
        tmp_path / "r",
        sources=f"open_pdks {SHA}\n",
        store={"version": OTHER, "sha256": BUNDLE},
    )
    code, report, err = _run(capsys, tmp_path, "check", "--expect-version", SHA)
    assert code == 0 and err == ""
    # byte-identical to an install with no .pdk-store.json at all
    assert report["version_expectation"] == {
        "status": "match",
        "expected": SHA,
        "installed": f"open_pdks {SHA}",
    }
    code, report, _ = _run(capsys, tmp_path, "check", "--expect-version", OTHER)
    assert code == 6
    assert report["version_expectation"]["installed"] == f"open_pdks {SHA}"


# --------------------------------------------------------------------------- #
# Issue #5624: ``check --expect-bundle-sha256``
# --------------------------------------------------------------------------- #


def test_bundle_match_exits_0(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    code, report, err = _run(
        capsys, tmp_path, "check", "--expect-bundle-sha256", BUNDLE
    )
    assert code == 0 and err == ""
    assert report["bundle_expectation"] == {
        "status": "match",
        "expected": BUNDLE,
        "installed": BUNDLE,
    }


def test_bundle_mismatch_exits_6(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    code, report, err = _run(
        capsys, tmp_path, "check", "--expect-bundle-sha256", BUNDLE_OTHER
    )
    assert code == 6
    assert BUNDLE_OTHER in err and BUNDLE in err
    assert report["bundle_expectation"]["status"] == "mismatch"


@pytest.mark.parametrize(
    "store", [None, {"version": OTHER}, {"sha256": 123}, {"sha256": "  "}]
)
def test_bundle_unknown_fails_closed_exit_6(capsys, tmp_path, store):
    _install(tmp_path / "r", store=store)
    code, report, err = _run(
        capsys, tmp_path, "check", "--expect-bundle-sha256", BUNDLE
    )
    assert code == 6 and "unknown" in err
    assert report["bundle_expectation"]["installed"] is None


def test_bundle_absent_from_payload_without_flag(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    code, report, _ = _run(capsys, tmp_path, "check")
    assert code == 0 and "bundle_expectation" not in report
    code, report, _ = _run(capsys, tmp_path, "check", "--expect-version", OTHER)
    assert code == 0 and "bundle_expectation" not in report


def test_bundle_is_check_only_find_rejects_it(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    with pytest.raises(SystemExit) as exc:
        main(["pdk", "find", "--expect-bundle-sha256", BUNDLE])
    assert exc.value.code == 2


def test_both_expectation_flags_together(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    code, report, err = _run(
        capsys,
        tmp_path,
        "check",
        "--expect-version",
        OTHER[:9],
        "--expect-bundle-sha256",
        BUNDLE,
    )
    assert code == 0 and err == ""
    assert report["version_expectation"]["status"] == "match"
    assert report["bundle_expectation"]["status"] == "match"
    # one failing leg still exits 6 and reports both legs' verdicts
    code, report, err = _run(
        capsys,
        tmp_path,
        "check",
        "--expect-version",
        SHA,
        "--expect-bundle-sha256",
        BUNDLE_OTHER,
    )
    assert code == 6
    assert report["version_expectation"]["status"] == "mismatch"
    assert report["bundle_expectation"]["status"] == "mismatch"
    assert SHA in err and BUNDLE_OTHER in err


def test_bundle_text_output_line(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE})
    code = main(
        [
            "pdk",
            "check",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(tmp_path / "r"),
            "--expect-bundle-sha256",
            BUNDLE_OTHER,
        ]
    )
    out, err = capsys.readouterr()
    assert code == 6
    assert "bundle_expectation: mismatch" in out
    assert "klt: PDK bundle expectation mismatch" in err


def test_broken_symlink_wins_over_bundle_mismatch(capsys, tmp_path):
    _install(tmp_path / "r", store={"version": OTHER, "sha256": BUNDLE}, broken=True)
    code, report, err = _run(
        capsys, tmp_path, "check", "--expect-bundle-sha256", BUNDLE_OTHER
    )
    assert code == 4
    assert report["bundle_expectation"]["status"] == "mismatch"
    assert BUNDLE_OTHER in err
