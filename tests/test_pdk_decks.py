"""`klt pdk decks` and the deck-global scanner (issue #3816)."""

import json

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.drc_klayout_engine import scan_deck_globals

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
