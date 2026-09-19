"""Tests for `klt drc`'s opt-in ``klayout`` engine (issue #565): a subprocess
wrapper around the standalone ``klayout`` application binary that runs a
PDK-native DRC-DSL script (``.lydrc``/``.drc``) instead of this repo's own
curated `DrcRule` tables.

Mirrors `tests/test_lvs.py`'s ``"netgen"`` engine coverage pattern (issue
#343) one layer over: unit tests for the missing-binary, timeout, and
malformed/unparseable-report failure modes are all driven through a stubbed
``klayout_tools.drc.subprocess.run`` (no real ``klayout`` binary required),
plus a real-binary integration tier gated by
``skipif(shutil.which("klayout") is None, ...)`` -- mirroring
`tests/test_lvs.py`'s ``HAVE_NETGEN``/``_SKIP_NO_NETGEN`` pattern.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import klayout.db as kdb
import pytest

import klayout_tools.drc as drc_module
from helpers.subprocess_fakes import fake_completed
from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.drc import DrcError, run_drc_klayout_engine

#: Real-binary integration gate, mirroring `tests/test_lvs.py`'s
#: `HAVE_NETGEN`/`_SKIP_NO_NETGEN` pattern for netgen.
HAVE_KLAYOUT_BINARY = shutil.which("klayout") is not None
_SKIP_NO_KLAYOUT_BINARY = pytest.mark.skipif(
    not HAVE_KLAYOUT_BINARY, reason="klayout binary is not installed on this machine"
)

# --------------------------------------------------------------------------- #
# Fixtures: layouts, deck files, and RDB report text
# --------------------------------------------------------------------------- #


def _write_gds(path: Path, *, layer=(1, 0), box=(0, 0, 100, 1000)) -> str:
    """A trivial one-shape GDS -- content is irrelevant to the mocked-
    subprocess tests below (the stub never actually reads it), but
    `run_drc_klayout_engine` validates/loads it before launching the
    subprocess, so it must be a real, readable layout stream."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    li = layout.layer(*layer)
    top.shapes(li).insert(kdb.Box(*box))
    layout.write(str(path))
    return str(path)


def _write_deck_file(path: Path, text: str = "# stub deck\n") -> str:
    """`run_drc_klayout_engine` only checks `deck_file` exists before
    launching the subprocess -- its content is never read when the
    subprocess itself is mocked."""
    path.write_text(text, encoding="utf-8")
    return str(path)


_EMPTY_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
 </items>
</report-database>
"""

# Category declarations without findings do not prove which rules ran. The
# `output(...)` call, so KLayout declared one `<category>` per rule, and none
# of them produced an item. Structurally distinct from `_EMPTY_RDB` above --
# same zero violations, but this report can say what it looked at.
_CLEAN_WITH_CATEGORIES_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category>
   <name>W.1</name>
   <description>min width</description>
  </category>
  <category>
   <name>S.1</name>
   <description>min space</description>
  </category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
 </items>
</report-database>
"""

# One `edge-pair:` violation (a width/space-style check) under a quoted
# category name (`'W.1'`) -- both forms (quoted/unquoted) are verified
# against real `klayout -b -r ...` output, see `_strip_rdb_quotes`'s
# docstring in `drc.py`.
_EDGE_PAIR_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category>
   <name>W.1</name>
   <description>min width</description>
  </category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
  <item>
   <category>'W.1'</category>
   <cell>TOP</cell>
   <values>
    <value>edge-pair: (0,0;0,1)|(0.2,1;0.2,0)</value>
   </values>
  </item>
 </items>
</report-database>
"""

# One `polygon:` violation under an unquoted category name (`L1`, no period
# -- KLayout's RDB writer only quotes a category name containing a `.`, per
# `_strip_rdb_quotes`'s docstring).
_POLYGON_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category>
   <name>L1</name>
   <description>raw layer 1</description>
  </category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
  <item>
   <category>L1</category>
   <cell>TOP</cell>
   <values>
    <value>polygon: (0,0;0,1;0.2,1;0.2,0)</value>
   </values>
  </item>
 </items>
</report-database>
"""

_TWO_VIOLATIONS_SAME_RULE_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category>
   <name>W.1</name>
   <description>min width</description>
  </category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
  <item>
   <category>'W.1'</category>
   <cell>TOP</cell>
   <values>
    <value>edge-pair: (0,0;0,1)|(0.2,1;0.2,0)</value>
   </values>
  </item>
  <item>
   <category>'W.1'</category>
   <cell>TOP</cell>
   <values>
    <value>edge-pair: (1,0;1,1)|(1.2,1;1.2,0)</value>
   </values>
  </item>
 </items>
</report-database>
"""


def _stub_klayout_drc_subprocess(
    monkeypatch,
    *,
    write_report: bool = True,
    rdb_xml: str = _EMPTY_RDB,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    side_effect: BaseException | None = None,
    captured_cmds: list | None = None,
):
    """Stub `drc.subprocess.run` for the klayout engine path -- mirrors
    `tests/test_lvs.py`'s `_stub_netgen_subprocess` for the netgen engine.

    The real `run_drc_klayout_engine` derives its own report path (a temp
    dir it owns) and passes it as a `-rd report=<path>` pair, so the fake
    locates it by scanning `cmd` for the `report=`-prefixed token rather
    than assuming a fixed position.
    """

    def fake_run(cmd, capture_output, text, timeout):
        if captured_cmds is not None:
            captured_cmds.append(cmd)
        if side_effect is not None:
            raise side_effect
        if write_report:
            report_arg = next(a for a in cmd if a.startswith("report="))
            report_path = report_arg.split("=", 1)[1]
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write(rdb_xml)
        return fake_completed(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(drc_module.subprocess, "run", fake_run)


# --------------------------------------------------------------------------- #
# `run_drc_klayout_engine` -- mocked-subprocess unit tests
# --------------------------------------------------------------------------- #


def test_klayout_engine_clean_report(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["schema_version"] == 2
    assert report["file"] == gds
    assert report["deck"] == deck_file
    assert report["engine"] == "klayout"
    assert report["dbu_um"] == 0.001
    assert report["status"] == "coverage_unknown"
    assert report["violation_count"] == 0
    assert report["rule_counts"] == {}
    assert report["violations"] == []
    assert report["coverage"] == {
        "deck_layers": [],
        "layers_checked": [],
        # An empty category table cannot establish whether rules executed.
        "rules_checked": [],
        "layers_in_stream_without_rules": [],
        "rules_skipped": [],
        "voltage_domain_warnings": [],
        "deck_scope": [],
        "nothing_checked": False,
        "schema_version": 1,
        "known": False,
        "checked": [],
        "skipped": [],
        "inapplicable": [],
        "unknown": [{"id": "klayout:execution", "reason": "unmeasured_rule_execution"}],
        "rule_categories": [],
        "nothing_checked_reasons": [],
    }
    assert report["provenance"]["deck"]["name"] == "deck.lydrc"
    assert report["provenance"]["deck"]["content_hash"].startswith("sha256:")
    assert report["provenance"]["input"]["content_hash"].startswith("sha256:")
    assert report["provenance"]["pdk"] is None


def test_klayout_engine_all_rules_gated_off_reports_nothing_checked(
    tmp_path, monkeypatch
):
    """An empty external RDB establishes unknown execution, not known zero."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["status"] == "coverage_unknown"
    assert report["violation_count"] == 0
    assert report["coverage"]["rules_checked"] == []
    assert report["coverage"]["nothing_checked"] is False
    assert report["coverage"]["nothing_checked_reasons"] == []


def test_klayout_engine_deck_var_set_reports_rules_checked(tmp_path, monkeypatch):
    """Declared categories remain unknown execution even when enable flags are set."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file, deck_vars={"feol": "true"})

    assert report["status"] == "coverage_unknown"
    assert report["violation_count"] == 0
    # Sorted, deduplicated -- the deck's own category names.
    assert report["coverage"]["rules_checked"] == []
    assert report["coverage"]["rule_categories"] == ["S.1", "W.1"]
    assert report["coverage"]["known"] is False
    assert report["coverage"]["nothing_checked"] is False
    assert report["coverage"]["nothing_checked_reasons"] == []


def test_klayout_engine_violations_are_never_nothing_checked(tmp_path, monkeypatch):
    """A report that found violations plainly checked something, so
    `nothing_checked` is `False` and `rules_checked` names the rule that
    fired -- even if that rule's category were somehow undeclared, the item's
    own category id is unioned in (issue #1996)."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EDGE_PAIR_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["status"] == "violations"
    assert report["coverage"]["rules_checked"] == ["W.1"]
    assert report["coverage"]["nothing_checked"] is False
    assert report["coverage"]["nothing_checked_reasons"] == []


def test_klayout_engine_edge_pair_violation_bbox_converted_to_dbu(
    tmp_path, monkeypatch
):
    """RDB values are reported in micrometres (user units); this engine
    converts them back to the input layout's own database units, matching
    `run_drc`'s existing integer-dbu `bbox` convention."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EDGE_PAIR_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["status"] == "violations"
    assert report["violation_count"] == 1
    assert report["rule_counts"] == {"W.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "W.1"
    assert violation["description"] == "min width"
    assert violation["check"] == "external"
    assert violation["layer"] == "W.1"
    assert violation["cell"] == "TOP"
    assert violation["source_cell"] is None
    assert violation["source_path"] is None
    # (0,0;0,1)|(0.2,1;0.2,0) um at dbu=0.001 -> (0,0)-(200,1000) dbu.
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 200, "top": 1000}
    # An edge-pair does not describe a simple polygon outline -- `polygon`
    # stays `None`, matching `run_drc`'s own "degenerate edge pair" case.
    assert violation["polygon"] is None


def test_klayout_engine_polygon_violation_populates_polygon_field(
    tmp_path, monkeypatch
):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_POLYGON_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    (violation,) = report["violations"]
    assert violation["rule"] == "L1"
    assert violation["description"] == "raw layer 1"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 200, "top": 1000}
    assert violation["polygon"] == [[0, 0], [0, 1000], [200, 1000], [200, 0]]


def test_klayout_engine_multiple_violations_same_rule_counted(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_TWO_VIOLATIONS_SAME_RULE_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["violation_count"] == 2
    assert report["rule_counts"] == {"W.1": 2}
    # Deterministic sort, matching `run_drc`'s own ordering discipline.
    lefts = [v["bbox"]["left"] for v in report["violations"]]
    assert lefts == sorted(lefts)


def test_klayout_engine_invokes_expected_command_shape(tmp_path, monkeypatch):
    captured: list = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=captured)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    run_drc_klayout_engine(gds, deck_file, timeout_s=42.0)

    (cmd,) = captured
    assert cmd[0] == "klayout"
    assert cmd[1] == "-b"
    assert cmd[2] == "-r"
    assert cmd[3] == deck_file
    assert f"input={gds}" in cmd
    assert any(a.startswith("report=") for a in cmd)


def test_klayout_engine_deck_vars_appended_as_extra_rd_pairs(tmp_path, monkeypatch):
    """issue #1302: extra `-rd NAME=VALUE` script globals, beyond the
    always-set `input`/`report` pair, must be threaded through to the
    subprocess so a PDK-native deck gated behind FEOL/BEOL/metal-stack
    toggles can actually run its rules instead of silently checking
    nothing."""
    captured: list = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=captured)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    run_drc_klayout_engine(
        gds, deck_file, deck_vars={"feol": "true", "metal_top": "6LM"}
    )

    (cmd,) = captured
    assert f"input={gds}" in cmd
    assert any(a.startswith("report=") for a in cmd)
    assert "feol=true" in cmd
    assert "metal_top=6LM" in cmd
    # Extra vars are appended *after* input/report, as additional `-rd`
    # pairs -- same mechanical pattern as the existing pairs.
    feol_idx = cmd.index("feol=true")
    assert cmd[feol_idx - 1] == "-rd"


def test_klayout_engine_no_deck_vars_unaffected(tmp_path, monkeypatch):
    """Regression: the zero-config path (no deck_vars) is unaffected --
    only `input`/`report` are ever passed as `-rd` pairs."""
    captured: list = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=captured)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    run_drc_klayout_engine(gds, deck_file)

    (cmd,) = captured
    rd_values = [a for a in cmd if "=" in a]
    assert len(rd_values) == 2
    assert f"input={gds}" in rd_values
    assert any(a.startswith("report=") for a in rd_values)


def test_klayout_engine_deck_vars_recorded_in_provenance_deck_options(
    tmp_path, monkeypatch
):
    """issue #1306: `deck_vars` must reach `provenance.deck.options` so a
    committed report records *which* `--deck-var` configuration produced
    it -- reusing the same `build_provenance(deck_options=...)` mechanism
    already wired for `klt extract --deck-option`/`klt lvs` (issue #595)."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(
        gds, deck_file, deck_vars={"feol": "true", "metal_top": "6LM"}
    )

    assert report["provenance"]["deck"]["options"] == {
        "feol": "true",
        "metal_top": "6LM",
    }


def test_klayout_engine_no_deck_vars_provenance_omits_options_key(
    tmp_path, monkeypatch
):
    """Regression: the zero-config path (no deck_vars) must leave
    `provenance.deck` byte-identical to today's -- no `options` key at
    all, matching `build_provenance`'s existing omit-when-empty
    convention."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert "options" not in report["provenance"]["deck"]


def _make_pdk_install(tmp_path, variant: str) -> str:
    """Minimal on-disk PDK install `find_pdk` can resolve -- mirrors
    `tests/test_extract.py`'s helper of the same name/shape."""
    root = tmp_path / "pdk_install"
    (root / variant / "libs.tech").mkdir(parents=True)
    return str(root)


def test_klayout_engine_pdk_flags_populate_provenance_pdk(tmp_path, monkeypatch):
    """Issue #1901: `--pdk`/`--pdk-root` must be recorded in
    `provenance.pdk` for the `klayout` engine too -- *in addition to*, not
    instead of, this engine's existing (separate) use of the same flags to
    resolve the native deck script via `pdk.drc_deck_file` one layer up in
    `cli/drc_cmd.py`."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")
    root = _make_pdk_install(tmp_path, "sky130A")

    report = run_drc_klayout_engine(
        gds, deck_file, pdk_variant="sky130A", pdk_root=root
    )

    assert report["provenance"]["pdk"] == {
        "name": "sky130A",
        "source": report["provenance"]["pdk"]["source"],
        "version": None,
    }
    assert report["provenance"]["pdk"]["source"] is not None


def test_klayout_engine_no_pdk_flags_leaves_provenance_pdk_null(tmp_path, monkeypatch):
    """Regression guard: the existing `test_klayout_engine_clean_report`
    already asserts this for the zero-arg call; this pins it explicitly as
    its own test so it survives independently of that test's other
    assertions changing."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["provenance"]["pdk"] is None


def test_klayout_engine_unresolvable_pdk_still_fails(tmp_path, monkeypatch):
    """An unresolvable `--pdk`/`--pdk-root` must still fail the run (issue
    #1901's acceptance criteria), not silently resolve to `provenance.pdk:
    null`."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError):
        run_drc_klayout_engine(
            gds,
            deck_file,
            pdk_variant="not-a-real-variant",
            pdk_root=str(tmp_path / "empty-pdk-root"),
        )


def test_cli_klayout_engine_pdk_flag_populates_provenance(
    tmp_path, monkeypatch, capsys
):
    """CLI-level coverage: `klt drc --engine klayout --deck-file <path>
    --pdk <variant> --pdk-root <root> --format json` populates
    `.provenance.pdk` (issue #1901) via `drc_cmd.py::_run`'s threading of
    `args.pdk`/`args.pdk_root` into `run_drc_klayout_engine`. `--deck-file`
    is given explicitly so this stays hermetic (no dependency on the fake
    PDK install shipping a real `.lydrc` script `drc_deck_file` could
    resolve)."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")
    root = _make_pdk_install(tmp_path, "sky130A")

    assert (
        main(
            [
                "drc",
                gds,
                "--engine",
                "klayout",
                "--deck-file",
                deck_file,
                "--pdk",
                "sky130A",
                "--pdk-root",
                root,
                "--format",
                "json",
            ]
        )
        == 4
    )
    data = json.loads(capsys.readouterr().out)
    assert data["provenance"]["pdk"]["name"] == "sky130A"


def test_klayout_engine_missing_binary_raises_actionable_error(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(
        monkeypatch, side_effect=FileNotFoundError("no such file: klayout")
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="binary not found on PATH"):
        run_drc_klayout_engine(gds, deck_file)


def test_klayout_engine_timeout_raises(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(
        monkeypatch,
        side_effect=subprocess.TimeoutExpired(cmd=["klayout"], timeout=300.0),
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="did not complete within"):
        run_drc_klayout_engine(gds, deck_file)


def test_klayout_engine_no_report_file_raises(tmp_path, monkeypatch):
    """klayout -b -r can exit 0 even when the deck script errored out before
    reaching report(...) -- never trust the exit code alone. No report file
    at all must not be silently treated as a clean run."""
    _stub_klayout_drc_subprocess(
        monkeypatch, write_report=False, stdout="deck script raised an error\n"
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="did not produce a report file"):
        run_drc_klayout_engine(gds, deck_file)


# --------------------------------------------------------------------------- #
# A partially-executed deck must never read as clean (issue #1941)
#
# A PDK-native deck typically calls `report(...)` near the top and appends
# rules as they run, so a deck that aborts partway through (an unsupported
# DRC-DSL construct, a typo'd method) still leaves a well-formed report file
# behind covering only the rules that ran before the abort. The report file's
# presence alone is therefore not a completion signal -- klayout's own exit
# status and its `ERROR:` output are the two signals that distinguish a deck
# that finished from one that died mid-run.
# --------------------------------------------------------------------------- #

#: Real `klayout -b -r` output shape for a deck that calls an undefined
#: method after its own `report(...)` call (issue #1941's reproduction).
_DECK_ABORT_STDERR = (
    "ERROR: In repro.drc: undefined local variable or method "
    "`this_method_does_not_exist_in_the_drc_dsl' for main:Object\n"
    "ERROR: NameError: undefined local variable or method "
    "`this_method_does_not_exist_in_the_drc_dsl' in Executable::execute\n"
)


def test_klayout_engine_nonzero_exit_raises_even_with_report_file(
    tmp_path, monkeypatch
):
    """A deck that wrote its report file early and then aborted leaves a
    *partial* report behind -- a non-zero klayout exit status must fail the
    run rather than reporting the partial report's zero violations as
    `status: "clean"`."""
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stdout="whatever\n"
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="klayout reported an error"):
        run_drc_klayout_engine(gds, deck_file)


def test_klayout_engine_error_output_raises_even_when_exit_status_is_zero(
    tmp_path, monkeypatch
):
    """`klayout -b -r` can exit `0` on a failed deck, so the exit status is
    not a *necessary* signal either -- an `ERROR` line in its own output
    fails the run on its own."""
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=0, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="klayout reported an error"):
        run_drc_klayout_engine(gds, deck_file)


def test_klayout_engine_deck_error_message_carries_klayouts_own_output(
    tmp_path, monkeypatch
):
    """Same as the no-report-file branch: surface klayout's own output so
    the caller can see *which* construct the deck died on."""
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError) as excinfo:
        run_drc_klayout_engine(gds, deck_file)

    message = str(excinfo.value)
    assert "this_method_does_not_exist_in_the_drc_dsl" in message
    assert "exit status 1" in message
    assert "--allow-deck-errors" in message


def test_klayout_engine_rule_output_mentioning_error_is_not_a_deck_error(
    tmp_path, monkeypatch
):
    """Only a line *starting* with `ERROR` is klayout's own error prefix --
    a deck that echoes the word mid-line (a rule named `ERROR_...`, a
    progress line) must not be misread as a failed run."""
    _stub_klayout_drc_subprocess(
        monkeypatch,
        rdb_xml=_EMPTY_RDB,
        stdout="Executing rule ERROR_CHECK.1\n  ERROR_CHECK.1: 0 errors\n",
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    assert report["status"] == "coverage_unknown"
    assert "engine_deck_errors" not in report


def test_klayout_engine_clean_run_omits_engine_deck_errors_key(tmp_path, monkeypatch):
    """The additive `engine_deck_errors` key exists only on a run that
    actually tolerated deck errors -- a normal run's payload is unchanged."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    assert "engine_deck_errors" not in run_drc_klayout_engine(gds, deck_file)


def test_klayout_engine_allow_deck_errors_accepts_partial_report(tmp_path, monkeypatch):
    """The escape hatch for a caller who has deliberately scoped around a
    known-unrunnable rule: the partial report is accepted, but the run
    records what it tolerated so the verdict is never silently clean."""
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file, allow_deck_errors=True)

    assert report["status"] == "coverage_unknown"
    assert report["engine_deck_errors"]["exit_status"] == 1
    assert len(report["engine_deck_errors"]["error_lines"]) == 2
    assert report["engine_deck_errors"]["error_lines"][0].startswith(
        "ERROR: In repro.drc:"
    )


def test_klayout_engine_allow_deck_errors_still_requires_a_report_file(
    tmp_path, monkeypatch
):
    """The escape hatch tolerates a *partial* report, never a missing one --
    no report file at all remains an unconditional failure."""
    _stub_klayout_drc_subprocess(
        monkeypatch, write_report=False, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="did not produce a report file"):
        run_drc_klayout_engine(gds, deck_file, allow_deck_errors=True)


def test_cli_drc_klayout_engine_deck_error_exits_one(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().err)
    assert "klayout reported an error" in payload["error"]["message"]


def test_cli_drc_klayout_engine_allow_deck_errors_flag(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--allow-deck-errors",
            "--format",
            "json",
        ]
    )

    assert exit_code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "coverage_unknown"
    assert payload["engine_deck_errors"]["exit_status"] == 1


def test_cli_drc_klayout_engine_allow_deck_errors_text_output_warns(
    tmp_path, monkeypatch, capsys
):
    """`--format text` must not render a tolerated partial run as an
    unqualified `status: coverage_unknown` either."""
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--allow-deck-errors",
            "--format",
            "text",
        ]
    )

    assert exit_code == 4
    out = capsys.readouterr().out
    assert "deck errors tolerated" in out
    assert "exit status 1" in out


def test_cli_drc_klayout_engine_request_document_allow_deck_errors(
    tmp_path, monkeypatch, capsys
):
    """The request-document form mirrors the flag (issue #1867's rule that
    every flag is reachable as a field)."""
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_DECK_ABORT_STDERR
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.drc.request/1",
                "file": gds,
                "engine": "klayout",
                "deck_file": deck_file,
                "allow_deck_errors": True,
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["drc", str(request), "--format", "json"])

    assert exit_code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["engine_deck_errors"]["exit_status"] == 1


@_SKIP_NO_KLAYOUT_BINARY
def test_klayout_engine_real_binary_partial_deck_is_not_reported_clean(tmp_path):
    """Issue #1941's own reproduction, end to end against a real `klayout`:
    a deck that calls `report(...)` first and then hits an undefined method
    writes a report file and exits non-zero. It must fail the run, not
    report `status: "clean"`."""
    deck_file = _write_deck_file(
        tmp_path / "partial.drc",
        "source($input)\n"
        'report("partial", $report)\n'
        'input(1, 0).width(0.1).output("W.1", "min width")\n'
        "this_method_does_not_exist_in_the_drc_dsl\n",
    )
    gds = _write_gds(tmp_path / "input.gds", layer=(1, 0), box=(0, 0, 1000, 1000))

    with pytest.raises(DrcError, match="klayout reported an error"):
        run_drc_klayout_engine(gds, deck_file, timeout_s=60.0)


def test_klayout_engine_unparseable_report_raises_not_silently_clean(
    tmp_path, monkeypatch
):
    """The exact failure mode this issue exists to catch: malformed report
    XML must never silently produce `status: "clean"` -- the parser fails
    loud instead."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml="not even xml\n")
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="could not parse klayout DRC report"):
        run_drc_klayout_engine(gds, deck_file)


def test_klayout_engine_top_unsupported_raises(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(monkeypatch)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="does not support --top"):
        run_drc_klayout_engine(gds, deck_file, top="TOP")


def test_klayout_engine_missing_deck_file_raises(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(monkeypatch)
    gds = _write_gds(tmp_path / "test.gds")

    with pytest.raises(DrcError, match="deck file not found"):
        run_drc_klayout_engine(gds, str(tmp_path / "does-not-exist.lydrc"))


def test_klayout_engine_missing_input_file_raises_before_launching_subprocess(
    tmp_path, monkeypatch
):
    captured: list = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=captured)
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="file not found"):
        run_drc_klayout_engine(str(tmp_path / "does-not-exist.gds"), deck_file)

    # Fail-fast: the subprocess is never launched for a bad input path.
    assert captured == []


def test_klayout_engine_cleans_up_work_dir(tmp_path, monkeypatch):
    work_dirs: list = []
    real_mkdtemp = drc_module.tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        created = real_mkdtemp(*args, **kwargs)
        work_dirs.append(created)
        return created

    monkeypatch.setattr(drc_module.tempfile, "mkdtemp", spy_mkdtemp)
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EDGE_PAIR_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    run_drc_klayout_engine(gds, deck_file)

    (work_dir,) = work_dirs
    assert not Path(work_dir).exists()


def test_klayout_engine_cleans_up_work_dir_on_error(tmp_path, monkeypatch):
    """The `finally`-scoped cleanup must run on the error path too, not
    just the success path -- mirrors `_cleanup_netgen_work_dir`'s own
    `finally` placement in `lvs.py`."""
    work_dirs: list = []
    real_mkdtemp = drc_module.tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        created = real_mkdtemp(*args, **kwargs)
        work_dirs.append(created)
        return created

    monkeypatch.setattr(drc_module.tempfile, "mkdtemp", spy_mkdtemp)
    _stub_klayout_drc_subprocess(monkeypatch, write_report=False)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError, match="did not produce a report file"):
        run_drc_klayout_engine(gds, deck_file)

    (work_dir,) = work_dirs
    assert not Path(work_dir).exists()


# --------------------------------------------------------------------------- #
# `klt drc --engine klayout` -- CLI dispatch
# --------------------------------------------------------------------------- #


def test_cli_drc_curated_engine_still_requires_deck(tmp_path, capsys):
    gds = _write_gds(tmp_path / "test.gds")

    exit_code = main(["drc", gds, "--format", "json"])

    assert exit_code == 1
    payload = capsys.readouterr().err
    assert "--deck is required" in payload


def test_cli_drc_klayout_engine_no_pdk_install_resolves_raises(
    tmp_path, monkeypatch, capsys
):
    """No `--deck-file` and `--pdk-root` names a location with no PDK
    install at all: `pdk.find_pdk` itself raises `PdkNotFoundError`, which
    `drc_cmd.run` must catch and turn into the same clean error envelope
    (exit 1), never a traceback."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])
    gds = _write_gds(tmp_path / "test.gds")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--pdk-root",
            str(tmp_path / "no-such-pdk-root"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    payload = capsys.readouterr().err
    assert "no supported-layout PDK install" in payload


def test_cli_drc_klayout_engine_pdk_found_but_no_native_deck_raises(
    tmp_path, monkeypatch, capsys
):
    """The PDK install itself resolves, but its `libs.tech/klayout` asset
    directory has no `drc/` subdirectory (or no single ready-to-run script
    inside it, e.g. a gf180mcu-shaped fragments-only layout) -- a distinct
    failure mode from "no PDK install at all" above, with its own actionable
    message pointing at `--deck-file`."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])
    root = tmp_path / "install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)  # no klayout/ asset dir
    gds = _write_gds(tmp_path / "test.gds")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--pdk-root",
            str(root),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    payload = capsys.readouterr().err
    assert "no PDK-native klayout DRC deck script found" in payload


def test_cli_drc_klayout_engine_with_explicit_deck_file(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EDGE_PAIR_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--format",
            "json",
        ]
    )

    assert exit_code == 3  # violations found
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["engine"] == "klayout"
    assert payload["deck"] == deck_file
    assert payload["violation_count"] == 1


def test_cli_drc_klayout_engine_unknown_execution_exits_four(
    tmp_path, monkeypatch, capsys
):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--format",
            "text",
        ]
    )

    assert exit_code == 4
    out = capsys.readouterr().out
    assert "engine: klayout" in out
    assert "status: coverage_unknown" in out


def test_cli_drc_klayout_engine_deck_var_reaches_subprocess(tmp_path, monkeypatch):
    """issue #1302: `--deck-var NAME=VALUE` (repeatable) reaches the
    subprocess as extra `-rd` pairs."""
    captured: list = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=captured)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--deck-var",
            "feol=true",
            "--deck-var",
            "beol=true",
            "--format",
            "json",
        ]
    )

    assert exit_code == 4
    (cmd,) = captured
    assert "feol=true" in cmd
    assert "beol=true" in cmd


def test_cli_drc_klayout_engine_malformed_deck_var_raises(
    tmp_path, monkeypatch, capsys
):
    """A `--deck-var` value missing the `=` separator is a clean error
    (exit 1), not a `KeyError`/traceback."""
    _stub_klayout_drc_subprocess(monkeypatch)
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--deck-var",
            "feol_no_equals",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    payload = capsys.readouterr().err
    assert "invalid --deck-var" in payload


def test_cli_drc_deck_var_ignored_for_curated_engine(tmp_path, capsys):
    """`--deck-var` combined with `--engine curated` (the default) is a
    clean no-op -- ignored, not rejected, the same treatment
    `--deck-file`/`--timeout-s` already get for that engine mismatch. The
    existing `--deck is required` error still fires normally, proving
    `--deck-var` was never consulted on this path."""
    gds = _write_gds(tmp_path / "test.gds")

    exit_code = main(["drc", gds, "--deck-var", "feol_no_equals", "--format", "json"])

    assert exit_code == 1
    payload = capsys.readouterr().err
    assert "--deck is required" in payload
    assert "invalid --deck-var" not in payload


# --------------------------------------------------------------------------- #
# Real-binary integration tier (issue #565's own test-plan requirement) --
# skips cleanly when no `klayout` binary is on PATH, never blocking the PR
# on its absence.
# --------------------------------------------------------------------------- #

#: A minimal, self-contained KLayout DRC-DSL script -- no PDK dependency, so
#: this tier only requires the `klayout` binary itself (gated by
#: `_SKIP_NO_KLAYOUT_BINARY`), not a real PDK install. Mirrors the
#: `-rd input=... -rd report=...` invocation convention verified for this
#: issue against a real sky130A install's own `sky130A.lydrc` (see that
#: file's embedded usage comment, cited in `run_drc_klayout_engine`'s
#: docstring).
_MINIMAL_WIDTH_DECK = """
if $input
  source($input)
end
if $report
  report("minimal width check", $report)
end
l1 = input(1, 0)
l1.width(0.5.um).output("W.1", "minimum width 0.5um")
"""


@_SKIP_NO_KLAYOUT_BINARY
def test_klayout_engine_real_binary_reports_seeded_violation(tmp_path):
    deck_file = _write_deck_file(tmp_path / "minimal.drc", _MINIMAL_WIDTH_DECK)
    # 0.2um wide shape on layer (1, 0) -- narrower than the deck's 0.5um
    # minimum width threshold.
    gds = _write_gds(tmp_path / "violation.gds", layer=(1, 0), box=(0, 0, 200, 1000))

    report = run_drc_klayout_engine(gds, deck_file, timeout_s=60.0)

    assert report["engine"] == "klayout"
    assert report["status"] == "violations"
    assert report["violation_count"] == 1
    assert report["rule_counts"] == {"W.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "W.1"
    assert violation["description"] == "minimum width 0.5um"
    assert violation["cell"] == "TOP"
    assert violation["bbox"]["left"] == 0
    assert violation["bbox"]["right"] == 200


@_SKIP_NO_KLAYOUT_BINARY
def test_klayout_engine_real_binary_no_findings_still_has_unknown_coverage(tmp_path):
    deck_file = _write_deck_file(tmp_path / "minimal.drc", _MINIMAL_WIDTH_DECK)
    # 1.0um wide shape -- wider than the 0.5um minimum, so no violation.
    gds = _write_gds(tmp_path / "clean.gds", layer=(1, 0), box=(0, 0, 1000, 1000))

    report = run_drc_klayout_engine(gds, deck_file, timeout_s=60.0)

    assert report["status"] == "coverage_unknown"
    assert report["violation_count"] == 0

    assert report["coverage"]["known"] is False


@_SKIP_NO_KLAYOUT_BINARY
def test_klayout_engine_real_binary_cli_end_to_end(tmp_path, capsys):
    deck_file = _write_deck_file(tmp_path / "minimal.drc", _MINIMAL_WIDTH_DECK)
    gds = _write_gds(tmp_path / "violation.gds", layer=(1, 0), box=(0, 0, 200, 1000))

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--timeout-s",
            "60",
            "--format",
            "json",
        ]
    )

    assert exit_code == 3
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "violations"
    assert payload["violation_count"] == 1


# --------------------------------------------------------------------------- #
# `--engine klayout` against an SG13CMOS5L-shaped `--pdk` install (issue
# #1399)
#
# The tests above all drive the engine via an explicit `--deck-file`. This
# section instead exercises the `--pdk`/`--pdk-root` -> `pdk.drc_deck_file`
# resolution path end-to-end against a *fabricated* install matching
# IHP-Open-PDK's real, on-disk SG13CMOS5L layout (verified against a real
# fleet-host install, `~/share/pdk/ihp-sg13cmos5l`, commit `607e18d`,
# 2026-08-25): `libs.tech/klayout/tech/drc/ihp-sg13cmos5l.drc` -- one
# directory deeper than open_pdks' `libs.tech/klayout/drc/<variant>.lydrc`
# (`drc_deck_file`'s nested-`tech/drc/` fallback, see `tests/test_pdk.py`).
# The deck content itself is a minimal, syntactically-valid stand-in (same
# `_MINIMAL_WIDTH_DECK` used above) rather than the real ~500-line
# `ihp-sg13cmos5l.drc` -- running that PDK-specific deck for real is a
# separate, larger follow-up (a curated starter deck, tracked as part of
# #1398's decomposition), out of scope here. This test only proves the
# resolver + engine plumbing, not the PDK's own rule content.
# --------------------------------------------------------------------------- #


def _make_ihp_sg13cmos5l_shaped_install(root: Path) -> Path:
    """Fabricate a flat, IHP-Open-PDK-shaped `ihp-sg13cmos5l` install under
    ``root`` with a minimal, syntactically-valid native DRC deck standing in
    for the real ``ihp-sg13cmos5l.drc`` -- same shape as a real fetched
    install, but hermetic (no multi-hundred-MB fetch required)."""
    drc_dir = root / "libs.tech" / "klayout" / "tech" / "drc"
    drc_dir.mkdir(parents=True)
    _write_deck_file(drc_dir / "ihp-sg13cmos5l.drc", _MINIMAL_WIDTH_DECK)
    return root


@_SKIP_NO_KLAYOUT_BINARY
def test_klayout_engine_real_binary_against_sg13cmos5l_shaped_install(tmp_path):
    pdk_root = tmp_path / "ihp-sg13cmos5l"
    _make_ihp_sg13cmos5l_shaped_install(pdk_root)

    deck_file = pdk.drc_deck_file(root=str(pdk_root))
    assert deck_file == str(
        pdk_root / "libs.tech" / "klayout" / "tech" / "drc" / "ihp-sg13cmos5l.drc"
    )

    # 0.2um wide shape -- narrower than the deck's 0.5um minimum width.
    gds = _write_gds(tmp_path / "violation.gds", layer=(1, 0), box=(0, 0, 200, 1000))

    report = run_drc_klayout_engine(gds, deck_file, timeout_s=60.0)

    assert report["status"] == "violations"
    assert report["violation_count"] == 1
    assert report["rule_counts"] == {"W.1": 1}


@_SKIP_NO_KLAYOUT_BINARY
def test_cli_klayout_engine_real_binary_resolves_sg13cmos5l_via_pdk_flags(
    tmp_path, capsys
):
    pdk_root = tmp_path / "ihp-sg13cmos5l"
    _make_ihp_sg13cmos5l_shaped_install(pdk_root)
    # 1.0um wide shape -- wider than the 0.5um minimum, so no violation.
    gds = _write_gds(tmp_path / "clean.gds", layer=(1, 0), box=(0, 0, 1000, 1000))

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--pdk",
            "ihp-sg13cmos5l",
            "--pdk-root",
            str(pdk_root),
            "--timeout-s",
            "60",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "clean"
    assert payload["violation_count"] == 0
    assert payload["deck"] == str(
        pdk_root / "libs.tech" / "klayout" / "tech" / "drc" / "ihp-sg13cmos5l.drc"
    )
