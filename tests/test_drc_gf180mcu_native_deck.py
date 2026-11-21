"""Native gf180mcu family main script resolution for `klt drc` (issue #4002)."""

from __future__ import annotations

import argparse
import json

from klayout_tools import pdk
from klayout_tools.cli import drc_cmd, main
from klayout_tools.cli._drc_native import resolve_native_deck
from klayout_tools.pdk_native_drc import native_drc_main_script


def _install(tmp_path, variant: str, drc_files: tuple[str, ...]) -> str:
    root = tmp_path / "pdk_install"
    drc = root / variant / "libs.tech" / "klayout" / "tech" / "drc"
    drc.mkdir(parents=True)
    for name in drc_files:
        (drc / name).write_text("# stub\n")
    return str(root)


def _args(root, pdk_name, **kw):
    ns = dict(
        deck_file=None,
        deck_var=None,
        min_klayout_version=None,
        pdk=pdk_name,
        pdk_root=root,
    )
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_main_script_resolved_with_variant_default(tmp_path):
    root = _install(tmp_path, "gf180mcuD", ("gf180mcu.drc",))
    found = native_drc_main_script(variant="gf180mcuD", root=root)
    assert found["deck_file"].endswith("gf180mcu.drc")
    assert found["deck_vars"] == {"variant": "gf180mcuD"}
    assert found["min_klayout_version"] == "0.30.9"
    # drc_deck_file keeps its "None means no variant-named deck" contract.
    assert pdk.drc_deck_file(variant="gf180mcuD", root=root) is None


def test_missing_main_script_returns_none(tmp_path):
    root = _install(tmp_path, "gf180mcuD", ())
    assert native_drc_main_script(variant="gf180mcuD", root=root) is None


def test_sky130_resolution_unchanged(tmp_path):
    root = _install(tmp_path, "sky130A", ("sky130A.lydrc", "gf180mcu.drc"))
    deck, vars_, floor, native = resolve_native_deck(_args(root, "sky130A"))
    assert deck.endswith("sky130A.lydrc")
    assert (vars_, floor, native) == ({}, None, None)
    assert native_drc_main_script(variant="sky130A", root=root) is None


def test_deck_file_override_wins(tmp_path):
    root = _install(tmp_path, "gf180mcuD", ("gf180mcu.drc",))
    deck, vars_, floor, native = resolve_native_deck(
        _args(root, "gf180mcuD", deck_file="x.drc")
    )
    assert (deck, vars_, floor, native) == ("x.drc", {}, None, None)


def test_user_deck_var_and_floor_override_defaults(tmp_path):
    root = _install(tmp_path, "gf180mcuD", ("gf180mcu.drc",))
    deck, vars_, floor, native = resolve_native_deck(
        _args(root, "gf180mcuD", deck_var=["variant=E"], min_klayout_version="0.30.1")
    )
    assert vars_ == {"variant": "E"}
    assert floor == "0.30.1"
    assert native["deck_vars_defaulted"] == {}
    assert native["min_klayout_version_defaulted"] is False


def test_cli_klayout_engine_uses_main_script_and_reports_it(
    tmp_path, monkeypatch, capsys
):
    root = _install(tmp_path, "gf180mcuD", ("gf180mcu.drc",))
    seen = {}

    def fake_engine(path, deck_file, **kw):
        seen.update(deck_file=deck_file, **kw)
        return {"schema_version": 3, "status": "clean"}

    monkeypatch.setattr(drc_cmd, "run_drc_klayout_engine", fake_engine)
    gds = tmp_path / "a.gds"
    gds.write_bytes(b"")
    main(
        [
            "drc",
            str(gds),
            "--engine",
            "klayout",
            "--pdk",
            "gf180mcuD",
            "--pdk-root",
            root,
            "--format",
            "json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert seen["deck_vars"] == {"variant": "gf180mcuD"}
    assert seen["min_klayout_version"] == "0.30.9"
    assert out["native_deck"]["kind"] == "pdk_main_script"
    assert out["native_deck"]["min_klayout_version_defaulted"] is True


def _multi_install(tmp_path, variants: dict[str, tuple[str, ...]]) -> str:
    root = tmp_path / "pdk_multi"
    for variant, files in variants.items():
        drc = root / variant / "libs.tech" / "klayout" / "tech" / "drc"
        drc.mkdir(parents=True)
        for name in files:
            (drc / name).write_text("# stub\n")
    return str(root)


def test_native_deck_constrained_to_deck_family(tmp_path, monkeypatch):
    """Issue #5211: unset --pdk never yields another family's native deck."""
    monkeypatch.delenv("PDK", raising=False)
    from klayout_tools.cli._drc_native import native_family_error

    root = _multi_install(
        tmp_path,
        {"gf180mcuD": ("gf180mcu.drc",), "sky130A": ("sky130A.lydrc",)},
    )
    args = _args(root, None, deck="sg13g2")
    deck, _, _, native = resolve_native_deck(args)
    assert deck is None and native is None
    assert "sg13g2" in native_family_error(args)


def test_native_deck_matching_family_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("PDK", raising=False)
    root = _multi_install(
        tmp_path,
        {"gf180mcuD": ("gf180mcu.drc",), "sky130A": ("sky130A.lydrc",)},
    )
    deck, _, _, _ = resolve_native_deck(_args(root, None, deck="sky130"))
    assert deck.endswith("sky130A.lydrc")


def test_unknown_deck_name_unconstrained(tmp_path, monkeypatch):
    monkeypatch.delenv("PDK", raising=False)
    root = _multi_install(tmp_path, {"sky130A": ("sky130A.lydrc",)})
    deck, _, _, _ = resolve_native_deck(_args(root, None, deck="custom"))
    assert deck.endswith("sky130A.lydrc")


def test_explicit_pdk_family_mismatch_and_deck_file_bypass(tmp_path, monkeypatch):
    monkeypatch.delenv("PDK", raising=False)
    from klayout_tools.cli._drc_native import native_family_error

    root = _multi_install(tmp_path, {"sky130A": ("sky130A.lydrc",)})
    args = _args(root, "sky130A", deck="gf180mcu")
    assert resolve_native_deck(args)[0] is None
    assert "does not match" in native_family_error(args)
    bypass = _args(root, "sky130A", deck="gf180mcu", deck_file="x.drc")
    assert resolve_native_deck(bypass)[0] == "x.drc"
    assert native_family_error(bypass) is None
