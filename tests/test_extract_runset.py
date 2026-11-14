"""Tests for ``klt extract --engine klayout-runset`` (issue #5481).

Two layers, mirroring ``tests/test_lvs_klayout_runset.py``:

- The standalone ``klayout`` application is mocked at the ``subprocess.run``
  boundary; the fake "runset" writes a *real* ``.lvsdb`` built with the pip
  ``klayout.db`` API, so the read-the-database-and-write-SPICE path is
  exercised end to end without a PDK.
- Real-PDK tests against IHP's own ``pnpMPA`` LVS testcase, skipped when
  ``klayout``/the ``ihp-sg13cmos5l`` install/``ngspice`` is absent -- the same
  skip shape ``tests/test_lvs_runset_pnpmpa_cmos5l.py`` uses.
"""

from __future__ import annotations

import os
import subprocess

import klayout.db as kdb
import pytest

from klayout_tools import extract_runset, lvs_runset
from klayout_tools.extract import run_extract
from klayout_tools.extract_spef import ExtractError

# --------------------------------------------------------------------------- #
# Faked-subprocess layer
# --------------------------------------------------------------------------- #


def _make_gds(path: str, n_dev: int) -> None:
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    act, poly, bulk = ly.layer(1, 0), ly.layer(2, 0), ly.layer(9, 0)
    top.shapes(bulk).insert(kdb.Box(-1000, -1000, 100000, 2000))
    for i in range(n_dev):
        x = i * 2000
        top.shapes(act).insert(kdb.Box(x, 0, x + 1000, 400))
        top.shapes(poly).insert(kdb.Box(x + 400, -100, x + 600, 500))
    ly.write(path)


def _write_lvsdb(
    gds: str, schem: str, out: str, extract: bool = True, extra_circuit: bool = False
) -> None:
    """Stand in for a PDK runset: extract ``gds`` and write a real ``.lvsdb``.

    ``schem`` is read as the reference exactly as a runset would read the
    stub the engine synthesizes -- which is what makes the "an empty stub does
    not cost devices" property testable here as well as against the real PDK.
    """
    ly = kdb.Layout()
    ly.read(gds)
    l2n = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(ly, ly.top_cell(), []))
    act = l2n.make_layer(ly.layer(1, 0), "act")
    poly = l2n.make_layer(ly.layer(2, 0), "poly")
    bulk = l2n.make_layer(ly.layer(9, 0), "bulk")
    gate, sd = act & poly, act - poly
    if extract:
        l2n.extract_devices(
            kdb.DeviceExtractorMOS4Transistor("NMOS"),
            {"SD": sd, "G": gate, "tG": poly, "W": bulk},
        )
    for region in (sd, poly, gate, bulk):
        l2n.connect(region)
    l2n.extract_netlist()
    l2n.netlist().make_top_level_pins()
    ref = kdb.Netlist()
    ref.read(schem, kdb.NetlistSpiceReader())
    if extra_circuit:
        # A runset that left a second circuit behind (deep mode, or a sub-cell
        # `align` did not flatten into its caller).
        leftover = kdb.Circuit()
        leftover.name = "LEFTOVER"
        l2n.netlist().add(leftover)
    l2n.reference = ref
    l2n.compare(kdb.NetlistComparer())
    l2n.write(out)


@pytest.fixture
def fake_klayout(monkeypatch, tmp_path):
    """Replace the subprocess boundary; ``state`` controls the fake runset."""
    state: dict = {
        "returncode": 0,
        "write_db": True,
        "extract": True,
        "stdout": "",
        "timeout": False,
        "extra_circuit": False,
    }
    fake_bin = tmp_path / "klayout"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(fake_bin))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] != str(fake_bin):
            return real_run(cmd, *a, **kw)
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        if state["timeout"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1.0))
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        state["cmd"] = cmd
        state["args"] = args
        state["stub"] = open(args["schematic"]).read()
        if state["write_db"]:
            _write_lvsdb(
                args["input"],
                args["schematic"],
                args["report"],
                state["extract"],
                state["extra_circuit"],
            )
        return subprocess.CompletedProcess(
            cmd, state["returncode"], state["stdout"], ""
        )

    monkeypatch.setattr(lvs_runset.subprocess, "run", fake_run)
    return state


def _inputs(tmp_path, n_dev=1):
    gds = tmp_path / "l.gds"
    _make_gds(str(gds), n_dev)
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    return str(gds), str(deck)


def _run(tmp_path, n_dev=1, **extra):
    gds, deck = _inputs(tmp_path, n_dev)
    kwargs = {
        "output": str(tmp_path / "out.spice"),
        "engine": extract_runset.RUNSET_ENGINE,
        "deck_file": deck,
    }
    kwargs.update(extra)
    return run_extract(gds, None, **kwargs)


def test_extracts_devices_from_the_lvsdb(tmp_path, fake_klayout):
    report = _run(tmp_path, 2)
    assert report["engine"] == "klayout-runset"
    assert report["status"] == "extracted"
    assert report["device_count"] == 2
    assert report["device_counts"] == {"NMOS": 2}
    assert report["device_classes"] == ["NMOS"]
    assert report["top"] == "TOP"
    assert report["deck"] is None
    assert report["parasitics"] is None
    assert report["pdk"] is None
    assert report["metrics"]["extract__device__count"] == 2


def test_argument_construction(tmp_path, fake_klayout):
    _run(tmp_path, deck_vars={"run_mode": "flat", "feol": True})
    cmd = fake_klayout["cmd"]
    assert cmd[1:3] == ["-b", "-r"]
    assert "run_mode=flat" in cmd
    # A bool is lower-cased for the runset's own `bool_check?`.
    assert "feol=true" in cmd
    assert fake_klayout["args"]["top"] == "TOP"
    assert fake_klayout["args"]["topcell"] == "TOP"
    assert fake_klayout["args"]["report"].endswith(".lvsdb")


def test_synthesized_stub_is_an_empty_subckt_named_after_the_top(
    tmp_path, fake_klayout
):
    _run(tmp_path)
    stub = fake_klayout["stub"]
    assert ".SUBCKT TOP" in stub
    assert ".ENDS TOP" in stub
    # Nothing but comments and the empty subcircuit: a stub that declared a
    # device would make the discarded compare look meaningful.
    body = [
        line
        for line in stub.splitlines()
        if line.strip() and not line.startswith("*") and not line.startswith(".")
    ]
    assert body == []


def test_work_dir_is_removed(tmp_path, fake_klayout):
    _run(tmp_path)
    assert not os.path.exists(os.path.dirname(fake_klayout["args"]["schematic"]))


def test_devices_are_written_as_x_subcircuit_calls(tmp_path, fake_klayout):
    report = _run(tmp_path)
    text = open(report["netlist_path"]).read()
    cards = [ln for ln in text.splitlines() if ln and not ln.startswith(("*", "."))]
    assert len(cards) == 1
    assert cards[0].startswith("X")
    # The class name is the subcircuit name, and the terminal order is the
    # device class's own (S G D B for a MOS4 extractor).
    assert cards[0].endswith(" NMOS")
    assert "no parasitics" in text
    # The header has to say the call carries no device parameters too: the
    # netlist is what gets handed to `klt sim`, and a reader who never sees
    # warnings[] would otherwise read it as a faithful extraction.
    assert "no device parameters" in text


def test_device_model_renames_the_target_subcircuit(tmp_path, fake_klayout):
    report = _run(tmp_path, device_models={"NMOS": "pdk_nfet"})
    text = open(report["netlist_path"]).read()
    assert any(
        ln.startswith("X") and ln.endswith(" pdk_nfet") for ln in text.splitlines()
    )
    assert report["device_models"] == {"NMOS": "pdk_nfet"}


def test_dropped_parameters_are_named_in_warnings(tmp_path, fake_klayout):
    report = _run(tmp_path)
    assert len(report["warnings"]) == 1
    warning = report["warnings"][0]
    assert "NMOS" in warning
    assert " L" in warning and "W" in warning
    assert "devices[].params" in warning
    # The measured values are still reported, just not carried onto the card.
    assert report["devices"][0]["params"]


def test_no_devices_recognised_is_warned_about(tmp_path, fake_klayout):
    fake_klayout["extract"] = False
    report = _run(tmp_path)
    assert report["device_count"] == 0
    assert any("recognised no devices" in w for w in report["warnings"])


def test_extra_circuits_are_written_and_disclosed(tmp_path, fake_klayout):
    """A second circuit is kept in the written netlist -- dropping it would
    orphan any `X` call into it -- and the counts' scope is stated rather than
    silently narrowed."""
    fake_klayout["extra_circuit"] = True
    report = _run(tmp_path)
    assert report["device_count"] == 1
    assert report["top"] == "TOP"
    disclosure = [w for w in report["warnings"] if "circuits" in w]
    assert len(disclosure) == 1
    assert "LEFTOVER" in disclosure[0]
    assert "'TOP' alone" in disclosure[0]
    text = open(report["netlist_path"]).read()
    assert ".SUBCKT TOP" in text
    assert "LEFTOVER" in text


def test_no_netlist_writes_nothing(tmp_path, fake_klayout):
    report = _run(tmp_path, output=None, no_netlist=True)
    assert report["netlist_path"] is None
    assert report["netlist_sha256"] is None
    assert report["netlist_normalized_sha256"] is None
    assert report["device_count"] == 1
    assert not os.path.exists(str(tmp_path / "l.spice"))


def test_provenance_runset_block(tmp_path, fake_klayout):
    gds, deck = _inputs(tmp_path)
    report = run_extract(
        gds,
        None,
        output=str(tmp_path / "out.spice"),
        engine="klayout-runset",
        deck_file=deck,
        deck_vars={"run_mode": "flat"},
    )
    runset = report["provenance"]["runset"]
    assert runset["deck_file"] == deck
    assert runset["deck_vars"] == {"run_mode": "flat"}
    assert runset["content_hash"].startswith("sha256:")
    assert runset["klayout_application_version"] == "0.30.12"
    assert runset["exit_status"] == 0
    assert runset["engine_error_lines"] == []
    assert report["provenance"]["deck"]["name"] == "pdk.lvs"


def test_engine_error_lines_are_recorded(tmp_path, fake_klayout):
    fake_klayout["stdout"] = "INFO: fine\nERROR : something odd\n"
    report = _run(tmp_path)
    assert report["provenance"]["runset"]["engine_error_lines"] == [
        "ERROR : something odd"
    ]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(tmp_path / "nope"))
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path)
    assert "klayout" in str(exc.value)


def test_runset_wrote_no_database(tmp_path, fake_klayout):
    fake_klayout["write_db"] = False
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path)
    assert "did not write an .lvsdb database" in str(exc.value)


def test_nonzero_exit_without_database(tmp_path, fake_klayout):
    fake_klayout["write_db"] = False
    fake_klayout["returncode"] = 3
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path)
    assert "exited with status 3" in str(exc.value)


def test_nonzero_exit_with_a_database_still_extracts(tmp_path, fake_klayout):
    """The exit status proves nothing: a runset may exit non-zero because its
    (discarded) compare against the stub failed."""
    fake_klayout["returncode"] = 1
    report = _run(tmp_path)
    assert report["device_count"] == 1
    assert report["provenance"]["runset"]["exit_status"] == 1


def test_timeout(tmp_path, fake_klayout):
    fake_klayout["timeout"] = True
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path, runset_timeout=0.5)
    message = str(exc.value)
    assert "did not complete within 0.5s" in message
    # The shared launcher's hint names `klt lvs`' request field; this verb
    # points at its own flag instead.
    assert "--runset-timeout" in message
    assert "options.runset_timeout_s" not in message


def test_unreadable_database(tmp_path, fake_klayout, monkeypatch):
    def truncated(cmd, *a, **kw):
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        open(args["report"], "w").write("not a database\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(lvs_runset.subprocess, "run", truncated)
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path)
    assert "could not read the runset's .lvsdb database" in str(exc.value)


def test_deck_file_not_found(tmp_path, fake_klayout):
    gds, _ = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(
            gds, None, engine="klayout-runset", deck_file=str(tmp_path / "nope.lvs")
        )
    assert "--deck-file not found" in str(exc.value)


# --------------------------------------------------------------------------- #
# Flag validation
# --------------------------------------------------------------------------- #


def test_unknown_engine(tmp_path):
    with pytest.raises(ExtractError) as exc:
        run_extract(str(tmp_path / "x.gds"), "sky130", engine="nope")
    assert "unknown engine 'nope'" in str(exc.value)


def test_deck_file_requires_the_runset_engine(tmp_path):
    with pytest.raises(ExtractError) as exc:
        run_extract(str(tmp_path / "x.gds"), "sky130", deck_file="pdk.lvs")
    assert "--deck-file is only used by engine 'klayout-runset'" in str(exc.value)


def test_deck_var_requires_the_runset_engine(tmp_path):
    with pytest.raises(ExtractError) as exc:
        run_extract(str(tmp_path / "x.gds"), "sky130", deck_vars={"a": "b"})
    assert "--deck-var is only used by engine 'klayout-runset'" in str(exc.value)


def test_runset_engine_requires_a_deck_file(tmp_path):
    with pytest.raises(ExtractError) as exc:
        run_extract(str(tmp_path / "x.gds"), None, engine="klayout-runset")
    assert "requires --deck-file" in str(exc.value)


def test_deck_is_refused_under_the_runset_engine(tmp_path):
    gds, deck = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(gds, "sky130", engine="klayout-runset", deck_file=deck)
    assert "cannot honour --deck" in str(exc.value)


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"parasitics": True}, "--parasitics"),
        ({"critical_nets": ["a"]}, "--critical-net"),
        ({"deck_options": {"poly_res": "1k"}}, "--deck-option"),
        ({"subcircuit_cell": "SUB"}, "--subcircuit"),
        ({"hierarchical_cells": ("A",)}, "--hierarchical-cells"),
        ({"abstract_cell_patterns": ("A*",)}, "--abstract-cells"),
        ({"probes": [{"net": "a"}]}, "--probe"),
        ({"pdk_variant": "ihp-sg13cmos5l"}, "--pdk"),
        ({"spef_output": "o.spef"}, "--spef"),
        ({"declared_pins": frozenset({"A"})}, "--pins"),
        ({"rename_declared_pins": True}, "--rename-declared-pins"),
        ({"apply_resistor_fixed_offset": False}, "--defer-resistor-fixed-offset"),
    ],
)
def test_curated_only_flags_are_refused_by_name(tmp_path, kwargs, flag):
    gds, deck = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(gds, None, engine="klayout-runset", deck_file=deck, **kwargs)
    assert flag in str(exc.value)
    assert "cannot honour" in str(exc.value)


def test_several_refused_flags_are_all_named(tmp_path):
    gds, deck = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(
            gds,
            None,
            engine="klayout-runset",
            deck_file=deck,
            parasitics=True,
            distributed_rc=True,
        )
    assert "--parasitics" in str(exc.value)
    assert "--distributed-rc" in str(exc.value)


def test_reserved_deck_var_is_refused(tmp_path):
    gds, deck = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(
            gds,
            None,
            engine="klayout-runset",
            deck_file=deck,
            deck_vars={"topcell": "OTHER"},
        )
    assert "may not set 'topcell'" in str(exc.value)


def test_refused_flag_table_names_real_run_extract_parameters():
    """`run_extract` reads the refused set out of its own `locals()` against
    this table, so a key that is not a parameter would silently never be
    checked -- and a flag the runset engine cannot honour would be accepted
    and dropped, which is the one outcome issue #5481 rules out."""
    import inspect

    parameters = set(inspect.signature(run_extract).parameters)
    unknown = sorted(set(extract_runset.REFUSED_FLAG_BY_PARAM) - parameters)
    assert unknown == [], unknown


def test_curated_engine_is_unchanged_by_the_new_parameters():
    """Every refusable flag's default must read as "not given" -- otherwise a
    plain curated run would start refusing flags nobody passed."""
    assert (
        extract_runset.refused_flags(
            {
                "deck_name": None,
                "parasitics": False,
                "critical_nets": None,
                "abstract_cell_patterns": (),
                "deck_options": {},
                "probes": [],
                "apply_resistor_fixed_offset": True,
                "hierarchical_cells": (),
            }
        )
        == []
    )
