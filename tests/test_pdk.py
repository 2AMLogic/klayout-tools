"""Tests for PDK discovery/resolution (`klayout_tools.pdk` + `klt pdk`).

Every test runs against **fabricated** open_pdks-layout installs created under
``tmp_path`` — CI never downloads a real PDK. The environment is scrubbed and
the module's search-space constants are pointed away from the host by default
(the ``_isolate`` autouse fixture) so results are hermetic regardless of what
is installed on the machine running the suite.
"""

import json

import pytest

from klayout_tools import pdk
from klayout_tools.cli import main


def _make_install(root, variant, *, sources=None, assets=("ngspice",)):
    """Fabricate an open_pdks-layout variant under ``root``.

    Creates ``root/<variant>/libs.tech`` (the layout probe) plus a ``libs.tech``
    subdir per requested asset (or ``libs.ref`` for ``libs_ref``), and an
    optional ``SOURCES`` version stamp. Returns the variant directory.
    """
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    for asset in assets:
        if asset == "libs_ref":
            (variant_dir / "libs.ref").mkdir(parents=True, exist_ok=True)
        else:
            (variant_dir / "libs.tech" / asset).mkdir(parents=True, exist_ok=True)
    if sources is not None:
        (variant_dir / "SOURCES").write_text(sources, encoding="utf-8")
    return variant_dir


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars, redirect HOME, and empty the host search space.

    Individual tests opt back into stores/prefixes by setting the module
    constants (or ``$PDK_ROOT``) to controlled ``tmp_path`` locations.
    """
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


# --------------------------------------------------------------------------- #
# Resolution order / precedence
# --------------------------------------------------------------------------- #


def test_find_via_pdk_root_flag(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources="open_pdks abc123")

    report = pdk.find_pdk(root=str(root))

    assert report["schema_version"] == 1
    assert report["root"] == str(root)
    assert report["variant"] == "sky130A"
    assert report["version"] == "open_pdks abc123"
    assert report["resolved_via"] == "--pdk-root flag"


def test_find_via_pdk_root_env(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(root))

    report = pdk.find_pdk()

    assert report["variant"] == "sky130A"
    assert report["resolved_via"] == "PDK_ROOT environment variable"


def test_find_via_ciel_store(tmp_path, monkeypatch):
    # Step 3: ~/.ciel resolves via HOME expansion when earlier steps are absent.
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel", "~/.volare"])
    home = tmp_path / "home"
    _make_install(home / ".ciel", "sky130A")

    report = pdk.find_pdk()

    assert report["root"] == str(home / ".ciel")
    assert report["resolved_via"] == "search root: ~/.ciel"


def test_find_via_conventional_prefix(tmp_path, monkeypatch):
    # Step 4: ~/share/pdk resolves when stores are empty.
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", ["~/share/pdk"])
    home = tmp_path / "home"
    _make_install(home / "share" / "pdk", "sky130A")

    report = pdk.find_pdk()

    assert report["root"] == str(home / "share" / "pdk")
    assert report["resolved_via"] == "search root: ~/share/pdk"


def test_earlier_step_beats_later(tmp_path, monkeypatch):
    # Both $PDK_ROOT (step 2) and a store (step 3) hold installs: step 2 wins.
    env_root = tmp_path / "env"
    _make_install(env_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(env_root))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    _make_install(tmp_path / "home" / ".ciel", "sky130B")

    report = pdk.find_pdk()

    assert report["resolved_via"] == "PDK_ROOT environment variable"
    assert report["variant"] == "sky130A"


def test_pdk_root_nonexistent_falls_through(tmp_path, monkeypatch):
    # $PDK_ROOT set but pointing at a nonexistent dir: skip to the store.
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    _make_install(tmp_path / "home" / ".ciel", "sky130A")

    report = pdk.find_pdk()

    assert report["resolved_via"] == "search root: ~/.ciel"


# --------------------------------------------------------------------------- #
# Variant selection ($PDK vs --pdk)
# --------------------------------------------------------------------------- #


def test_pdk_env_selects_variant(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    _make_install(root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setenv("PDK", "sky130B")

    report = pdk.find_pdk()

    assert report["variant"] == "sky130B"


def test_pdk_flag_beats_pdk_env(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    _make_install(root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setenv("PDK", "sky130B")

    report = pdk.find_pdk(variant="sky130A")

    assert report["variant"] == "sky130A"


def test_default_variant_is_first_sorted(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130B")
    _make_install(root, "sky130A")

    report = pdk.find_pdk(root=str(root))

    assert report["variant"] == "sky130A"


def test_requested_variant_absent_raises(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A")

    with pytest.raises(pdk.PdkNotFoundError) as excinfo:
        pdk.find_pdk(variant="sky130B", root=str(root))

    assert "sky130B" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Layout probe + version stamp
# --------------------------------------------------------------------------- #


def test_dir_without_libs_tech_is_not_a_variant(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    # A sibling directory with no libs.tech/ must be ignored.
    (root / "not-a-variant").mkdir()
    (root / "not-a-variant" / "libs.ref").mkdir()

    report = pdk.list_pdks(root=str(root))

    names = [v["name"] for v in report["installs"][0]["variants"]]
    assert names == ["sky130A"]


def test_version_null_when_no_sources(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources=None)

    report = pdk.find_pdk(root=str(root))

    assert report["version"] is None


def test_version_multiline_sources_joined(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources="open_pdks abc\n\nsky130 def\n")

    report = pdk.find_pdk(root=str(root))

    assert report["version"] == "open_pdks abc; sky130 def"


def test_assets_present_and_absent(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice", "klayout", "libs_ref"))

    assets = pdk.find_pdk(root=str(root))["assets"]

    assert assets["ngspice"] == str(root / "sky130A" / "libs.tech" / "ngspice")
    assert assets["klayout"] == str(root / "sky130A" / "libs.tech" / "klayout")
    assert assets["libs_ref"] == str(root / "sky130A" / "libs.ref")
    # Keys are always present; absent tools resolve to None.
    assert assets["xschem"] is None
    assert assets["magic"] is None
    assert assets["netgen"] is None


# --------------------------------------------------------------------------- #
# list_pdks
# --------------------------------------------------------------------------- #


def test_list_enumerates_multiple_installs_and_variants(tmp_path, monkeypatch):
    env_root = tmp_path / "env"
    _make_install(env_root, "sky130A", sources="v1")
    _make_install(env_root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(env_root))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    _make_install(tmp_path / "home" / ".ciel", "gf180mcuD")

    report = pdk.list_pdks()

    assert report["schema_version"] == 1
    assert len(report["installs"]) == 2
    first = report["installs"][0]
    assert first["resolved_via"] == "PDK_ROOT environment variable"
    assert [v["name"] for v in first["variants"]] == ["sky130A", "sky130B"]
    assert first["variants"][0]["version"] == "v1"
    second = report["installs"][1]
    assert second["resolved_via"] == "search root: ~/.ciel"
    assert [v["name"] for v in second["variants"]] == ["gf180mcuD"]


def test_list_empty_is_success(tmp_path):
    report = pdk.list_pdks()
    assert report == {"schema_version": 1, "installs": []}


# --------------------------------------------------------------------------- #
# No-install failure
# --------------------------------------------------------------------------- #


def test_find_no_install_raises_with_search_order(tmp_path, monkeypatch):
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "nope"))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])

    with pytest.raises(pdk.PdkNotFoundError) as excinfo:
        pdk.find_pdk()

    message = str(excinfo.value)
    assert "PDK_ROOT environment variable" in message  # names the search order
    assert "~/.ciel" in message
    assert "ciel enable" in message  # actionable install pointer


# --------------------------------------------------------------------------- #
# CLI envelope conformance (via klt main())
# --------------------------------------------------------------------------- #


def test_cli_find_json_on_stdout(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources="open_pdks abc")

    exit_code = main(["pdk", "find", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["variant"] == "sky130A"
    assert payload["schema_version"] == 1


def test_cli_find_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(["pdk", "find", "--pdk-root", str(tmp_path / "nope"),
                      "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout empty on error
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "pdk find"
    assert "ciel enable" in error["error"]["message"]


def test_cli_list_empty_exits_zero(capsys):
    exit_code = main(["pdk", "list", "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"schema_version": 1, "installs": []}


def test_cli_env_exports_are_eval_able(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "sky130A")

    exit_code = main(["pdk", "env", "--pdk-root", str(root)])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"export PDK_ROOT={root}", "export PDK=sky130A"]


def test_cli_env_matches_find(tmp_path, capsys, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    _make_install(root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setenv("PDK", "sky130B")

    main(["pdk", "env"])
    env_out = capsys.readouterr().out
    main(["pdk", "find", "--format", "json"])
    find_payload = json.loads(capsys.readouterr().out)

    assert f"export PDK_ROOT={root}" in env_out
    assert "export PDK=sky130B" in env_out
    assert find_payload["variant"] == "sky130B"
    assert find_payload["root"] == str(root)


def test_cli_env_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(["pdk", "env", "--pdk-root", str(tmp_path / "nope"),
                      "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "pdk env"


def test_cli_pdk_no_subcommand_prints_help(capsys):
    exit_code = main(["pdk"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "find" in err and "list" in err and "env" in err
