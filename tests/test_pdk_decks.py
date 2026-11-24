"""`klt pdk decks` and the deck-global scanner (issue #3816)."""

import json

import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.drc_klayout_engine import (
    _missing_deck_globals_warning,
    scan_deck_globals,
)

_GATED_DECK = """\
# $commented is ignored
source($input, $top_cell) if $input
report("x", $report) if $report
if not defined? $feol
  $feol = "false"
end
$beol = "0" unless defined? $beol
$metal ||= '5LM'
if $feol == "true" && $seal
  logger.info("thr #{$thr}")
end
defined_only = defined? $unused_probe
puts $stdout
"""


def _make_drc_install(tmp_path, files):
    variant_dir = tmp_path / "sky130A"
    drc = variant_dir / "libs.tech" / "klayout" / "drc"
    drc.mkdir(parents=True)
    for name, text in files.items():
        (drc / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


def _by_name(globals_):
    return {g["name"]: g for g in globals_}


def test_scan_detects_defaults_and_required(tmp_path):
    deck = tmp_path / "d.drc"
    deck.write_text(_GATED_DECK, encoding="utf-8")

    found = _by_name(scan_deck_globals(str(deck)))

    assert found["feol"] == {
        "name": "feol",
        "required_by_caller": False,
        "default": "false",
    }
    assert found["beol"]["default"] == "0" and not found["beol"]["required_by_caller"]
    assert (
        found["metal"]["default"] == "5LM" and not found["metal"]["required_by_caller"]
    )
    assert found["seal"]["required_by_caller"] and found["seal"]["default"] is None
    assert found["thr"]["required_by_caller"]
    # Commented-out, io, and Ruby-builtin globals are excluded.
    for ignored in ("commented", "input", "report", "top_cell", "stdout"):
        assert ignored not in found


def test_scan_follows_includes(tmp_path):
    (tmp_path / "inc.rb").write_text("x = $from_include\n", encoding="utf-8")
    deck = tmp_path / "d.drc"
    deck.write_text('require_relative "inc"\n', encoding="utf-8")

    assert "from_include" in _by_name(scan_deck_globals(str(deck)))


def test_scan_unreadable_deck_is_empty(tmp_path):
    assert scan_deck_globals(str(tmp_path / "missing.drc")) == []


def test_decks_lists_kinds_default_and_globals(tmp_path):
    root = _make_drc_install(
        tmp_path,
        {
            "sky130A.lydrc": "<klayout-macro>$input $report</klayout-macro>\n",
            "sky130A_mr.drc": _GATED_DECK,
            "zeroarea.rb.drc": "$ignored_in_helper = 1\n",
            "klayout_gds_drc_check.py": "print(1)\n",
        },
    )

    report = pdk.drc_decks(root=root)

    assert report["schema_version"] == 1
    assert report["pdk"] == "sky130A"
    decks = {d["name"]: d for d in report["decks"]}
    # The Python driver is not a deck and is not listed.
    assert set(decks) == {"sky130A.lydrc", "sky130A_mr.drc", "zeroarea.rb.drc"}
    assert decks["sky130A.lydrc"]["kind"] == "lydrc"
    assert decks["sky130A.lydrc"]["is_default"] is True
    assert decks["sky130A.lydrc"]["globals"] == []
    assert decks["sky130A_mr.drc"]["kind"] == "drc"
    assert decks["sky130A_mr.drc"]["is_default"] is False
    assert "feol" in _by_name(decks["sky130A_mr.drc"]["globals"])
    assert decks["zeroarea.rb.drc"]["kind"] == "helper"
    assert decks["zeroarea.rb.drc"]["globals"] == []
    assert [d["name"] for d in report["decks"]] == sorted(decks)


def test_decks_without_drc_dir_is_empty_success(tmp_path):
    variant_dir = tmp_path / "sky130A"
    (variant_dir / "libs.tech" / "klayout").mkdir(parents=True)

    report = pdk.drc_decks(root=str(tmp_path))

    assert report["decks"] == []


def test_cli_decks_json(tmp_path, capsys):
    root = _make_drc_install(tmp_path, {"sky130A_mr.drc": _GATED_DECK})

    code = main(["pdk", "decks", "--pdk-root", root, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["decks"][0]["name"] == "sky130A_mr.drc"


def test_cli_decks_text(tmp_path, capsys):
    root = _make_drc_install(tmp_path, {"sky130A_mr.drc": _GATED_DECK})

    code = main(["pdk", "decks", "--pdk-root", root])

    text = capsys.readouterr().out
    assert code == 0
    assert "sky130A_mr.drc" in text and "seal*" in text


# --- LVS runsets and the helper / `||` default idioms (issue #5827) ---------

_LVS_RUNSET = """\
def bool_check?(obj, default: false)
  return default if obj.nil?
end
SIMPLIFY = bool_check?($simplify)
SPICE_NAMES = bool_check?($spice_net_names, default: true)
METAL_TOP = $metal_top || '9K'
MIM_CAP = $mim_cap || "2"
FINGERS = $fingers || 4
ok = defined?($probe)
exitstatus = $CHILD_STATUS.exitstatus
source($input, $topcell)
schematic = $schematic
puts $top
puts $needed
"""


def _make_lvs_install(tmp_path, files, nested=False):
    variant_dir = tmp_path / "sky130A"
    klayout = variant_dir / "libs.tech" / "klayout"
    lvs = klayout / "tech" / "lvs" if nested else klayout / "lvs"
    lvs.mkdir(parents=True)
    for name, text in files.items():
        (lvs / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_scan_helper_and_or_literal_defaults(tmp_path):
    deck = tmp_path / "r.lvs"
    deck.write_text(_LVS_RUNSET, encoding="utf-8")

    found = _by_name(scan_deck_globals(str(deck), runset_idioms=True))

    def pair(name):
        return found[name]["required_by_caller"], found[name]["default"]

    assert pair("simplify") == (False, "false")
    assert pair("spice_net_names") == (False, "true")
    assert pair("metal_top") == (False, "9K")
    assert pair("mim_cap") == (False, "2")
    assert pair("fingers") == (False, "4")
    assert pair("needed") == (True, None)
    # `defined?($x)` supplies no default; it is not the helper idiom.
    assert pair("probe") == (True, None)


def test_scan_ignores_child_status_and_extra_names(tmp_path):
    deck = tmp_path / "r.lvs"
    deck.write_text(_LVS_RUNSET, encoding="utf-8")

    assert "CHILD_STATUS" not in _by_name(scan_deck_globals(str(deck)))
    reserved = _by_name(
        scan_deck_globals(str(deck), extra_ignored=("schematic", "top", "topcell"))
    )
    assert not {"schematic", "top", "topcell"} & set(reserved)
    # Without the extra names the shared scan is unchanged.
    assert {"schematic", "top"} <= set(_by_name(scan_deck_globals(str(deck))))


def _lvs_entries(report):
    return [d for d in report["decks"] if d["kind"] == "lvs"]


def test_decks_lists_lvs_runsets_both_layouts(tmp_path_factory):
    for nested in (False, True):
        root = _make_lvs_install(
            tmp_path_factory.mktemp("lvs"),
            {
                "sky130.lvs": _LVS_RUNSET,
                "other.lvs": "$x_other = 1\n",
                "sky130.lylvs": "<klayout-macro/>\n",
                "run_lvs.py": "print(1)\n",
                "README.md": "docs\n",
            },
            nested=nested,
        )
        report = pdk.drc_decks(root=root)

        lvs = {d["name"]: d for d in _lvs_entries(report)}
        assert set(lvs) == {"other.lvs", "sky130.lvs"}
        assert lvs["sky130.lvs"]["is_default"] is True
        assert lvs["other.lvs"]["is_default"] is False
        assert set(lvs["sky130.lvs"]) == {
            "path",
            "name",
            "kind",
            "is_default",
            "globals",
        }
        names = set(_by_name(lvs["sky130.lvs"]["globals"]))
        assert "simplify" in names and "needed" in names
        assert not {"input", "schematic", "top", "topcell", "CHILD_STATUS"} & names


def test_decks_lvs_default_independent_of_drc_default(tmp_path):
    # DRC has no resolvable default here (no sky130A.lydrc/.drc), LVS does.
    root = _make_drc_install(tmp_path, {"sky130A_mr.drc": _GATED_DECK})
    lvs_dir = tmp_path / "sky130A" / "libs.tech" / "klayout" / "lvs"
    lvs_dir.mkdir()
    (lvs_dir / "sky130.lvs").write_text(_LVS_RUNSET, encoding="utf-8")

    report = pdk.drc_decks(root=root)

    by_kind = {d["name"]: d for d in report["decks"]}
    assert by_kind["sky130A_mr.drc"]["is_default"] is False
    assert by_kind["sky130.lvs"]["is_default"] is True
    # DRC entries come first and keep their pre-#5827 shape and values.
    assert [d["kind"] for d in report["decks"]] == ["drc", "lvs"]
    drc = _by_name(by_kind["sky130A_mr.drc"]["globals"])
    assert drc["feol"] == {
        "name": "feol",
        "required_by_caller": False,
        "default": "false",
    }
    assert drc["seal"] == {
        "name": "seal",
        "required_by_caller": True,
        "default": None,
    }


def test_decks_lvs_no_default_when_unresolvable_and_empty_dir(tmp_path):
    root = _make_lvs_install(tmp_path, {"unrelated.lvs": "$a = 1\n"})

    report = pdk.drc_decks(root=root)

    assert [(d["name"], d["is_default"]) for d in report["decks"]] == [
        ("unrelated.lvs", False)
    ]


def test_decks_empty_lvs_dir_and_unreadable_runset(tmp_path):
    root = _make_lvs_install(tmp_path, {})
    assert pdk.drc_decks(root=root)["decks"] == []

    lvs = tmp_path / "sky130A" / "libs.tech" / "klayout" / "lvs"
    (lvs / "sky130.lvs").symlink_to(tmp_path / "missing.lvs")
    # A dangling symlink is not a file: skipped, no traceback.
    assert pdk.drc_decks(root=root)["decks"] == []


def test_cli_decks_text_empty_message_names_both_kinds(tmp_path, capsys):
    variant_dir = tmp_path / "sky130A"
    (variant_dir / "libs.tech" / "klayout").mkdir(parents=True)

    code = main(["pdk", "decks", "--pdk-root", str(tmp_path)])

    text = capsys.readouterr().out
    assert code == 0
    assert "LVS" in text and "DRC" in text


def test_cli_decks_text_renders_lvs_rows(tmp_path, capsys):
    root = _make_lvs_install(tmp_path, {"sky130.lvs": _LVS_RUNSET})

    code = main(["pdk", "decks", "--pdk-root", root])

    text = capsys.readouterr().out
    assert code == 0
    assert "sky130.lvs" in text and "lvs" in text and "needed*" in text


# --- review fixes: DRC scan unchanged, qualified calls, comments, aliases ---

_DRC_SHAPED = (
    "MASK = $mask || 'metal1'\nENABLED = bool_check?($enabled, default: true)\n"
)


def test_drc_scan_keeps_helper_and_or_idioms_required(tmp_path):
    # The #5827 idioms are LVS-runset-only: a DRC deck using them keeps its
    # pre-#5827 rows (required, no default) and its missing-globals warning.
    root = _make_drc_install(tmp_path, {"sky130A.drc": _DRC_SHAPED})
    lvs_dir = tmp_path / "sky130A" / "libs.tech" / "klayout" / "lvs"
    lvs_dir.mkdir()
    (lvs_dir / "sky130.lvs").write_text(_DRC_SHAPED, encoding="utf-8")

    by_name = {d["name"]: d for d in pdk.drc_decks(root=root)["decks"]}

    assert _by_name(by_name["sky130A.drc"]["globals"]) == {
        "mask": {"name": "mask", "required_by_caller": True, "default": None},
        "enabled": {"name": "enabled", "required_by_caller": True, "default": None},
    }
    lvs = _by_name(by_name["sky130.lvs"]["globals"])
    assert (lvs["mask"]["required_by_caller"], lvs["mask"]["default"]) == (
        False,
        "metal1",
    )
    assert (lvs["enabled"]["required_by_caller"], lvs["enabled"]["default"]) == (
        False,
        "true",
    )
    deck = by_name["sky130A.drc"]["path"]
    assert _by_name(scan_deck_globals(deck)) == _by_name(
        by_name["sky130A.drc"]["globals"]
    )
    warnings = _missing_deck_globals_warning(deck, {})
    assert [w["category"] for w in warnings] == ["deck_globals_not_supplied"]


@pytest.mark.parametrize(
    "line",
    [
        "abort unless File.exist?($reference_file)",
        "ok = File.exist?($reference_file)",
        "ok = Foo::exist?($reference_file)",
        "ok = obj&.exist?($reference_file)",
    ],
)
def test_runset_receiver_qualified_predicate_stays_required(tmp_path, line):
    deck = tmp_path / "r.lvs"
    deck.write_text(line + "\n", encoding="utf-8")

    found = _by_name(scan_deck_globals(str(deck), runset_idioms=True))

    assert found["reference_file"] == {
        "name": "reference_file",
        "required_by_caller": True,
        "default": None,
    }


@pytest.mark.parametrize(
    ("line", "name", "expected"),
    [
        # A trailing comment must not hide a real default ...
        ("METAL = $metal || '9K' # top metal fallback", "metal", (False, "9K")),
        ("LAYERS = $layers || 6 # six metals", "layers", (False, "6")),
        ("SIMPLIFY = bool_check?($simplify) # off", "simplify", (False, "false")),
        (
            "NAMES = bool_check?($names, default: true) # keep",
            "names",
            (False, "true"),
        ),
        # ... nor invent one from commented-out code.
        ("puts $verbose # formerly bool_check?($verbose)", "verbose", (True, None)),
        (
            "puts $metal # METAL = $metal || '9K'",
            "metal",
            (True, None),
        ),
        # A `#` inside a quoted string is code, not a comment.
        ("METAL = $metal || '9K#1' # note", "metal", (False, "9K#1")),
        ('TAG = $tag || "a#b" # note', "tag", (False, "a#b")),
    ],
)
def test_runset_idioms_ignore_trailing_ruby_comments(tmp_path, line, name, expected):
    deck = tmp_path / "r.lvs"
    deck.write_text(line + "\n", encoding="utf-8")

    found = _by_name(scan_deck_globals(str(deck), runset_idioms=True))

    assert (found[name]["required_by_caller"], found[name]["default"]) == expected


def test_decks_lvs_alias_symlink_is_listed_but_not_default(tmp_path):
    root = _make_lvs_install(tmp_path, {"sky130.lvs": _LVS_RUNSET})
    lvs = tmp_path / "sky130A" / "libs.tech" / "klayout" / "lvs"
    (lvs / "alias.lvs").symlink_to(lvs / "sky130.lvs")
    resolved = pdk.lvs_deck_file(root=root)
    assert resolved is not None and resolved.endswith("sky130.lvs")

    entries = {d["name"]: d for d in pdk.drc_decks(root=root)["decks"]}

    assert set(entries) == {"alias.lvs", "sky130.lvs"}
    assert entries["sky130.lvs"]["is_default"] is True
    assert entries["alias.lvs"]["is_default"] is False
    assert [d["name"] for d in entries.values() if d["is_default"]] == ["sky130.lvs"]
