"""Native gf180mcu family main script resolution for `klt drc` (issue #4002)."""

from __future__ import annotations

import argparse
import json

import pytest

from klayout_tools import pdk
from klayout_tools.cli import drc_cmd, main
from klayout_tools.cli._drc_native import resolve_native_deck
from klayout_tools.drc import DrcError
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


def test_deck_var_parsing_is_the_shared_helper(tmp_path):
    """`--deck-var` is parsed by `cli._parsing.parse_deck_options` (issue
    #6738), the same helper `klt lvs`/`klt extract` use: last-one-wins, only
    the first `=` splits, and whitespace around key and value is stripped."""
    root = _install(tmp_path, "sky130A", ("sky130A.lydrc",))
    _, vars_, _, _ = resolve_native_deck(
        _args(root, "sky130A", deck_var=["a=1", "b=x=y", "a=2", " c = z "])
    )
    assert vars_ == {"a": "2", "b": "x=y", "c": "z"}


@pytest.mark.parametrize("entry", ["novalue", "=v", "  =v"])
def test_malformed_deck_var_raises_drc_error(tmp_path, entry):
    """A `--deck-var` with no `=` or a blank key is a clean `DrcError` (exit
    1) before any engine runs. `klt drc` used to accept `=v` and forward a
    nameless `-rd =v` global to klayout; it now matches `klt lvs`/`extract`
    (issue #6738)."""
    root = _install(tmp_path, "sky130A", ("sky130A.lydrc",))
    with pytest.raises(DrcError) as excinfo:
        resolve_native_deck(_args(root, "sky130A", deck_var=[entry]))
    assert f"--deck-var entry {entry!r} is not KEY=VALUE" in str(excinfo.value)


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


def test_real_gf180_mt30_subset_cannot_earn_full_drc_credit(tmp_path):
    """Check registry IDs against the installed default, and grade the real envelope."""
    import os
    import shutil

    from klayout import db as kdb

    from klayout_tools.signoff import build_signoff
    from test_signoff import _item_3

    pdk_root = os.environ.get("PDK_ROOT")
    binary = shutil.which("klayout")
    if not pdk_root or not binary:
        pytest.skip("needs gf180mcuA and KLayout >= 0.30.9")
    deck = native_drc_main_script(variant="gf180mcuA", root=pdk_root)
    if not deck:
        pytest.skip("needs the gf180mcuA native deck")
    from klayout_tools.drc_klayout_app import _preflight_klayout_version

    try:
        _preflight_klayout_version(binary, "0.30.9")
    except DrcError as exc:
        pytest.skip(str(exc))
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("TOP")
    metal = cell.shapes(layout.layer(42, 0))
    gds = tmp_path / "m3.gds"
    metal.insert(kdb.Box(0, 0, 500, 10000))
    layout.write(str(gds))
    from klayout_tools.drc_klayout_engine import run_drc_klayout_engine

    def run(selector, expected=None, *, tolerate=False):
        return run_drc_klayout_engine(
            str(gds),
            deck["deck_file"],
            klayout_binary=binary,
            deck_vars={"variant": "gf180mcuA", "decks": selector},
            expected_rule_categories=expected,
            allow_deck_errors=tolerate,
        )

    narrow = run("metaltop_30k")
    broad = run("all,-mslot")
    assert "MT30.1a" in narrow["rule_counts"] and "MT30.1a" in broad["rule_counts"]
    s = narrow["native_deck"]["deck_selection"]
    b = broad["native_deck"]["deck_selection"]
    assert s["selected"] == s["started"] == ["metaltop_30k"]
    full = run("all", tolerate=True)["native_deck"]["deck_selection"]
    assert full["narrowed"] is False and full["omitted"] == []
    assert b["selected"] == b["started"]
    assert b["omitted"] == [f"mslot{i}" for i in range(1, 7)]
    assert set(b["selected"]) == set(full["selected"]) - set(b["omitted"])
    assert set(s["omitted"]) == set(full["selected"]) - {"metaltop_30k"}
    metal.clear()
    metal.insert(kdb.Box(0, 0, 4000, 10000))
    layout.write(str(gds))
    wide = run("metaltop_30k", 8)
    assert wide["status"] == "clean_partial" and wide["violation_count"] == 0
    from klayout_tools.coverage import build_check_coverage

    path = tmp_path / "wide.drc.json"
    for report in [
        wide,
        {
            **wide,
            "status": "clean",
            "coverage": build_check_coverage(checked=["MT30.1a"]),
        },
    ]:
        path.write_text(json.dumps(report))
        assert build_signoff([str(path)])["checks"][0]["passed"] is False
        item = _item_3(str(path))
        assert (item["status"], item["reason"], item["citation"]) == (
            "unmet",
            "partial_coverage",
            None,
        )


@pytest.mark.parametrize(
    ("selector", "payload", "expected"),
    [
        (
            None,
            '[["mt", "mslot1"], ["mt", "mslot1"]]',
            (["mt", "mslot1"], ["mt", "mslot1"]),
        ),
        ("mt", '[["mt", "mslot1"], ["mt"]]', (["mt", "mslot1"], ["mt"])),
        ("mt", "null", None),
        ("mt", '[["mt"], [1]]', None),
        ("mt", "truncated", None),
    ],
)
def test_registry_subprocess_contract(
    tmp_path, monkeypatch, selector, payload, expected
):
    from pathlib import Path

    from klayout_tools import drc_klayout_selection as selection

    directory = tmp_path / "installed deck"
    deck_file = directory / "gf180mcu.drc"

    def run(command, *, capture_output, check, timeout):
        assert command[:3] == ["klayout", "-b", "-r"]
        assert Path(command[3]).is_file()
        fields = dict(arg.split("=", 1) for arg in command[5::2])
        assert command[4::2] == ["-rd"] * len(fields)
        assert fields.pop("directory") == str(directory)
        output = Path(fields.pop("output"))
        assert fields == ({} if selector is None else {"selector": selector})
        assert capture_output and check and timeout == 30
        output.write_text(payload)

    monkeypatch.setattr(selection.subprocess, "run", run)
    assert (
        selection._registry("klayout", str(deck_file), selector, str(tmp_path), 60)
        == expected
    )


def test_registry_expands_installed_ids_without_running_drc(tmp_path):
    import shutil

    from klayout_tools.drc_klayout_selection import _registry

    binary = shutil.which("klayout")
    if binary is None:
        pytest.skip("needs a KLayout Ruby interpreter")
    (tmp_path / "rule_framework.rb").write_text(
        "module RuleFramework; class Registry < Hash; end; end"
    )
    (tmp_path / "options.rb").write_text("""
    module GF180DRC
      class Options
        DEFAULTS = {decks:'all'}
        def self.resolve_decks(tags, registry)
          out = []
          tags.split(',').each do |tag|
            group = registry.fetch(tag.delete_prefix('-'))
            out = tag.start_with?('-') ? out - group : out | group
          end
          out.map { |id| Struct.new(:id).new(id) }
        end
      end
    end
    """)
    decks = tmp_path / "rule_decks"
    decks.mkdir()
    (decks / "groups.rb").write_text("""
      slots = (1..3).map { |i| "mslot#{i}" }
      groups = {'mslot'=>slots, 'mt'=>['mt'], 'all'=>slots+['mt','spare']}
      GF180_DRC_REGISTRY.merge!(groups)
    """)
    script = tmp_path / "gf180mcu.drc"
    all_ids = ["mslot1", "mslot2", "mslot3", "mt", "spare"]
    for selector, selected in [
        (None, all_ids),
        ("all", all_ids),
        ("mt", ["mt"]),
        ("all,-mslot", ["mt", "spare"]),
    ]:
        assert _registry(binary, script, selector, tmp_path, 10) == (all_ids, selected)
    assert _registry(binary, script, "no-such-tag", tmp_path, 10) is None


@pytest.mark.parametrize(
    "registry,log",
    [
        (
            (["mt", "mslot1"], ["mt"]),
            "Starting DRC: executing 1 deck(s).\nExecuting deck mt from /mt.rb\n",
        ),
        (None, "Executing deck mt from /mt.rb\n"),
        ((["mt", "mslot1"], ["mt", "mslot1"]), ""),
    ],
)
def test_native_zero_finding_envelope_with_uncertified_scope(
    tmp_path, monkeypatch, registry, log
):
    import subprocess
    from pathlib import Path

    from klayout_tools import drc_klayout_engine as engine
    from klayout_tools import drc_klayout_selection
    from klayout_tools.coverage import build_check_coverage, work_id
    from klayout_tools.drc_klayout_engine import run_drc_klayout_engine
    from klayout_tools.signoff import build_signoff
    from test_drc_klayout_engine import _write_deck_file, _write_gds
    from test_signoff import _item_3

    monkeypatch.setattr(drc_klayout_selection, "_registry", lambda *args: registry)
    xml = (
        "<report-database><categories><category><name>MT30.1a</name>"
        "</category></categories><items/></report-database>"
    )

    def launch(*args, report_path, **kwargs):
        Path(report_path).write_text(xml)
        return subprocess.CompletedProcess(args, 0, log, "")

    monkeypatch.setattr(engine, "launch_klayout_deck", launch)
    monkeypatch.setattr(engine, "_preflight_klayout_version", lambda *args: "0.30.12")
    report = run_drc_klayout_engine(
        _write_gds(tmp_path / "test.gds"),
        _write_deck_file(tmp_path / "gf180mcu.drc"),
        deck_vars={"decks": "mt" if registry and len(registry[1]) == 1 else "all"},
        expected_rule_categories=1,
    )
    assert report["status"] != "clean"
    selection = report["native_deck"]["deck_selection"]
    if registry is None or not log:
        assert selection["not_reached"] is None
        assert report["coverage"]["unknown"]
    else:
        assert selection["omitted"] == ["mslot1"]
        assert report["coverage"]["skipped"] == [
            {"id": work_id("native_deck", "mslot1"), "reason": "not_selected"}
        ]
        assert report["status"] == "clean_partial"
        report.update(
            status="clean", coverage=build_check_coverage(checked=["MT30.1a"])
        )
    path = tmp_path / "subset.drc.json"
    path.write_text(json.dumps(report))
    assert build_signoff([str(path)])["checks"][0]["passed"] is False
    item = _item_3(str(path))
    assert item["status"] == "unmet" and item["citation"] is None
