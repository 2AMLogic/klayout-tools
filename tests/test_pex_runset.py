"""``klt pex --engine klayout-runset`` (issue #6559): the runset-engine route.

Same faked-subprocess layer as ``tests/test_extract_runset.py`` (whose
``fake_klayout`` fixture and ``_inputs`` this file reuses): the runset
subprocess is stubbed, the extraction, the pex envelope and the CLI are real.
The fake runset extracts ``NMOS`` devices, whose KLayout parameters are
``L W AS AD PS PD``. No ngspice, no PDK and no fleet is involved: delta mode is
exercised through ``--measure-command`` (a tiny Python harness), which the pex
envelope grades exactly as it does testbench legs.

One PDK-gated test at the end runs a **single** local ``klt pex --extract-only``
on IHP's ``pnpMPA`` testcase; it skips without the ``ihp-sg13cmos5l`` install.
"""

# ruff: noqa: F401, F811
from __future__ import annotations

import json
import os
import shutil

import pytest

from klayout_tools.cli import main, pex_cmd
from klayout_tools.cli.parser import create_parser
from klayout_tools.pex import PexError, run_pex
from klayout_tools.pex_runset import PexRunset, extraction_fields, runset_from_args
from klayout_tools.signoff import build_signoff, build_tier_report
from test_extract_runset import _inputs, _run
from test_extract_runset import fake_klayout as fake_klayout
from test_pex import _write_harness, resistor_layout
from test_signoff import PEX_PASS_ENVELOPE, _manifest, _write

NMOS_PARAMS = ["AD", "AS", "L", "PD", "PS", "W"]
W_L = {"W": ("w", "u"), "L": ("l", "u")}
ALL_PARAMS = {
    p: (p.lower(), "u") for p in NMOS_PARAMS
}  # every measured parameter declared


def _extract_only(tmp_path, *, runset=None, deck=None, n_dev=1, **kwargs):
    """One ``run_pex --extract-only`` through the faked runset."""
    gds, deck_file = _inputs(tmp_path, n_dev)
    runset = runset or PexRunset(deck_file=deck_file)
    return run_pex(
        gds,
        [],
        deck,
        output=str(tmp_path / "out.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
        runset=runset,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The route and its machine-readable disclosure
# --------------------------------------------------------------------------- #


def test_runset_route_states_engine_and_that_no_parasitics_are_carried(
    tmp_path, fake_klayout
):
    report = _extract_only(tmp_path, n_dev=2)

    extraction = report["extraction"]
    assert extraction["engine"] == "klayout-runset"
    assert extraction["parasitics_carried"] is False
    assert extraction["device_counts"] == {"NMOS": 2}
    assert extraction["device_count"] == 2
    # No curated deck ran and no parasitics model exists to echo.
    assert extraction["deck"] is None
    assert extraction["model"] is None
    assert extraction["netlist_adaptation"] is None
    assert extraction["critical_nets"] == []
    # The runset itself is attributable through the report's own provenance.
    assert report["provenance"]["runset"]["content_hash"].startswith("sha256:")
    # The envelope is otherwise the ordinary extract-only one.
    assert report["measurement"]["mode"] == "extract-only"
    assert report["status"] == "not_checked"


def test_params_not_carried_names_every_measured_parameter_by_default(
    tmp_path, fake_klayout
):
    report = _extract_only(tmp_path)

    assert report["extraction"]["params_not_carried"] == {"NMOS": NMOS_PARAMS}


def test_declared_device_params_shrink_params_not_carried(tmp_path, fake_klayout):
    report = _extract_only(
        tmp_path,
        runset=PexRunset(deck_file=_inputs(tmp_path)[1], device_params={"NMOS": W_L}),
    )

    assert report["extraction"]["params_not_carried"] == {
        "NMOS": ["AD", "AS", "PD", "PS"]
    }


def test_params_not_carried_is_empty_once_every_parameter_is_declared(
    tmp_path, fake_klayout
):
    """The field flips with no pex change once #5513's declarations cover it."""
    report = _extract_only(
        tmp_path,
        runset=PexRunset(
            deck_file=_inputs(tmp_path)[1], device_params={"NMOS": ALL_PARAMS}
        ),
    )

    assert report["extraction"]["params_not_carried"] == {}


def test_body_bias_is_not_assessed_for_a_runset_leg(tmp_path, fake_klayout):
    """The runset engine never runs the curated deck's floating-body check, so
    `"biased"` would state a clean result nothing measured."""
    report = _extract_only(tmp_path)

    assert report["body_bias"] == {
        "status": "not_assessed",
        "unbiased_device_count": None,
        "unbiased_nets": None,
        "unbiased_pmos_body_nets": None,
    }


def test_extraction_fields_are_empty_for_any_other_engine():
    assert extraction_fields({"device_counts": {"nmos": 1}}) == {}
    assert extraction_fields({"engine": "klayout", "device_counts": {}}) == {}


#: `extraction`'s keys for a curated-deck `--extract-only` run as they stood
#: before this route existed (pinned from `run_pex`'s source at the time); the
#: default engine must neither gain nor lose one.
CURATED_EXTRACTION_KEYS = {
    "deck",
    "netlist_adaptation",
    "device_count",
    "net_count",
    "netlist_sha256",
    "model",
    "critical_nets",
    "distributed_rc",
    "mom_rlc_override",
    "label_layers",
    "device_models",
    "device_classes",
    "unsupported_devices",
    "rename_declared_pins",
    "tie_declared_pins",
    "pdk",
    "parasitic_integrity",
}


def test_default_engine_envelope_gains_no_runset_fields(tmp_path, resistor_layout):
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
    )

    assert set(report["extraction"]) == CURATED_EXTRACTION_KEYS
    assert report["body_bias"]["status"] == "biased"
    assert "params_not_carried" not in report["extraction"]


# --------------------------------------------------------------------------- #
# Refusals -- by name, never silently ignored
# --------------------------------------------------------------------------- #

#: run_pex keyword -> the flag spelling the refusal must name. Every one is a
#: parasitic-only (or curated-deck-only) option `run_extract`'s own refusal
#: table rejects under the runset engine.
REFUSED_BY_NAME = [
    ({"critical_nets": ["n1"]}, "--critical-net"),
    ({"critical_net_patterns": ["n*"]}, "--critical-net-pattern"),
    ({"critical_net_all": True}, "--critical-net-all"),
    ({"critical_net_excludes": ["x*"]}, "--critical-net-exclude"),
    ({"distributed_rc": True}, "--distributed-rc"),
    ({"resistance_graph": True}, "--resistance-graph"),
    ({"resistance_graph_r_cut_ohm": 1.0}, "--resistance-graph-r-cut"),
    ({"parasitic_variants": ["no-coupling"]}, "--parasitic-variant"),
    ({"substrate_tie_ohms": 5.0}, "--substrate-tie-ohms"),
    ({"mom_rlc_net": "n1"}, "--mom-rlc-net"),
    ({"mom_rlc_resistance_ohm": 1.0}, "--mom-rlc-resistance-ohm"),
    ({"mom_rlc_capacitance_ff": 1.0}, "--mom-rlc-capacitance-ff"),
    ({"mom_rlc_inductance_nh": 1.0}, "--mom-rlc-inductance-nh"),
    ({"deck_options": {"poly_res": "2k"}}, "--deck-option"),
    ({"declared_pins": frozenset({"A"})}, "--pins"),
    ({"rename_declared_pins": True}, "--rename-declared-pins"),
    ({"tie_declared_pins": True}, "--tie-declared-pins"),
    ({"label_layers": {"poly": (1, 0)}}, "--label-layer"),
    ({"pdk_variant": "sky130A"}, "--pdk"),
    ({"pdk_root": "/nonexistent"}, "--pdk-root"),
]


@pytest.mark.parametrize(("kwargs", "flag"), REFUSED_BY_NAME)
def test_parasitic_only_option_is_refused_by_name(tmp_path, fake_klayout, kwargs, flag):
    with pytest.raises(PexError, match=rf"cannot honour .*{flag}\b"):
        _extract_only(tmp_path, **kwargs)
    # Refused before the runset ever launched: nothing was extracted.
    assert "cmd" not in fake_klayout


def test_curated_deck_name_is_refused_by_name_under_the_runset_engine(
    tmp_path, fake_klayout
):
    with pytest.raises(PexError, match=r"cannot honour --deck\b"):
        _extract_only(tmp_path, deck="sky130")


def test_every_refusal_is_named_at_once(tmp_path, fake_klayout):
    with pytest.raises(PexError) as excinfo:
        _extract_only(tmp_path, distributed_rc=True, critical_nets=["n1"])

    message = str(excinfo.value)
    assert "--distributed-rc" in message
    assert "--critical-net" in message


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"device_only_leg": True}, "--device-only-leg"),
        ({"tie_nets": [("a", "b")]}, "--tie-net"),
    ],
)
def test_options_run_extract_cannot_see_are_refused_by_name(
    tmp_path, fake_klayout, kwargs, flag
):
    """`--device-only-leg` and `--tie-net` are not `run_extract` parameters, so
    its refusal table never sees them; the route refuses them itself."""
    gds, deck = _inputs(tmp_path)
    with pytest.raises(PexError, match=rf"cannot honour {flag}"):
        # Testbench mode: `--extract-only` would reject `--device-only-leg`
        # as a conflict before the route's own refusal is reached.
        run_pex(
            gds,
            [str(tmp_path / "tb.json")],
            None,
            artifacts_dir=str(tmp_path / "art"),
            runset=PexRunset(deck_file=deck),
            **kwargs,
        )
    assert "cmd" not in fake_klayout


def test_backend_batch_is_not_refused_because_pex_extraction_is_local(
    tmp_path, fake_klayout
):
    """`klt extract --engine klayout-runset` refuses `--backend batch` because
    it would ship the extraction off-host, where the standalone klayout binary
    is not. `klt pex --backend` only reaches the `klt sim` legs; its own
    extraction always runs here -- so refusing it would block the fleet-routed
    multi-corner sim path for nothing."""
    report = _extract_only(tmp_path, backend="batch")

    assert report["extraction"]["engine"] == "klayout-runset"


# --------------------------------------------------------------------------- #
# model_mismatch: the device-set disclosure carries into the runset route
# --------------------------------------------------------------------------- #


def _reference(tmp_path, *cards, include=None):
    lines = [".SUBCKT TOP A B C D", "X1 A B C D NMOS", *cards]
    if include:
        lines.insert(1, f'.include "{include}"')
    lines.append(".ENDS TOP")
    path = tmp_path / "reference.spice"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_agreeing_reference_reports_no_model_mismatch(tmp_path, fake_klayout):
    report = _extract_only(tmp_path, reference_netlist=_reference(tmp_path))

    assert report["model_mismatch"] is None


def test_unrecognised_family_is_named_in_extract_only_mode(tmp_path, fake_klayout):
    """The runset recognised NMOS only; the reference also declares a `pnpMPA`
    -- the family a mask idiom (the `nsd_drw`/`nsd_block` `pnp_exclude` case)
    can make the runset silently miss. It must be named, not silently absent."""
    report = _extract_only(
        tmp_path, reference_netlist=_reference(tmp_path, "XQ1 A B C pnpMPA")
    )

    mismatch = report["model_mismatch"]
    assert mismatch is not None
    assert mismatch["kind"] == "model_set"
    assert mismatch["reference_only"] == ["pnpMPA"]
    assert mismatch["extracted_only"] == []
    assert "pnpMPA" not in mismatch["counts"]["extracted"]
    assert report["extraction"]["device_counts"] == {"NMOS": 1}


def test_unrecognised_family_is_named_through_an_include(tmp_path, fake_klayout):
    (tmp_path / "extra.spice").write_text("XQ1 A B C pnpMPA\n")
    report = _extract_only(
        tmp_path, reference_netlist=_reference(tmp_path, include="extra.spice")
    )

    assert report["model_mismatch"]["reference_only"] == ["pnpMPA"]


def test_unrecognised_family_is_named_in_delta_mode(tmp_path, fake_klayout):
    """Delta mode (`--measure-command`): reference vs the runset-extracted leg."""
    gds, deck = _inputs(tmp_path)
    report = run_pex(
        gds,
        [],
        None,
        output=str(tmp_path / "out.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=_reference(tmp_path, "XQ1 A B C pnpMPA"),
        runset=PexRunset(deck_file=deck),
    )

    assert report["measurement"]["mode"] == "command"
    assert report["delta"], "delta mode produced rows"
    assert report["extraction"]["engine"] == "klayout-runset"
    assert report["model_mismatch"]["reference_only"] == ["pnpMPA"]


def test_device_params_on_the_cards_do_not_disturb_model_mismatch(
    tmp_path, fake_klayout
):
    """`X... NMOS l=.. w=..`: trailing `name=value` tokens are not the model."""
    report = _extract_only(
        tmp_path,
        runset=PexRunset(deck_file=_inputs(tmp_path)[1], device_params={"NMOS": W_L}),
        reference_netlist=_reference(tmp_path),
    )

    assert report["model_mismatch"] is None


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


def _cli(tmp_path, *argv, capsys):
    gds, deck = _inputs(tmp_path)
    code = main(
        [
            "pex",
            gds,
            *[a.replace("DECKFILE", deck) for a in argv],
            "--output",
            str(tmp_path / "out.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, json.loads(captured.err or "null")


def test_cli_runset_route_needs_no_deck_and_emits_the_disclosure(
    tmp_path, fake_klayout, capsys
):
    code, out, _err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 4  # extract-only: ran, graded nothing (`not_checked`)
    extraction = json.loads(out)["extraction"]
    assert extraction["engine"] == "klayout-runset"
    assert extraction["parasitics_carried"] is False
    assert extraction["device_counts"] == {"NMOS": 1}


def test_cli_text_output_names_the_engine(tmp_path, fake_klayout, capsys):
    gds, deck = _inputs(tmp_path)
    code = main(
        [
            "pex",
            gds,
            "--engine",
            "klayout-runset",
            "--deck-file",
            deck,
            "--extract-only",
            "--output",
            str(tmp_path / "out.spice"),
            "--outdir",
            str(tmp_path / "art"),
        ]
    )

    assert code == 4
    assert "engine: klayout-runset  parasitics: none" in capsys.readouterr().out


def test_cli_device_param_reaches_the_cards_and_the_disclosure(
    tmp_path, fake_klayout, capsys
):
    code, out, _err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--device-param",
        "NMOS:W=w:u",
        "--device-param",
        "NMOS:L=l:u",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 4
    assert json.loads(out)["extraction"]["params_not_carried"] == {
        "NMOS": ["AD", "AS", "PD", "PS"]
    }
    assert "l=0.2u w=0.4u" in (tmp_path / "out.spice").read_text()


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["--engine", "klayout-runset", "--extract-only"], "requires --deck-file"),
        (
            ["--deck", "sky130", "--deck-file", "DECKFILE", "--extract-only"],
            "--deck-file is only used",
        ),
        (
            ["--deck", "sky130", "--deck-var", "a=b", "--extract-only"],
            "--deck-var is only used",
        ),
        (
            ["--deck", "sky130", "--device-param", "NMOS:W=w", "--extract-only"],
            "only used by engine",
        ),
        (
            ["--deck", "sky130", "--runset-timeout", "5", "--extract-only"],
            "only used by engine",
        ),
        (
            [
                "--engine",
                "klayout-runset",
                "--deck-file",
                "DECKFILE",
                "--deck-var",
                "input=x",
                "--extract-only",
            ],
            "may not set 'input'",
        ),
        (
            [
                "--engine",
                "klayout-runset",
                "--deck-file",
                "DECKFILE",
                "--deck-var",
                "novalue",
                "--extract-only",
            ],
            "not KEY=VALUE",
        ),
        (
            [
                "--engine",
                "klayout-runset",
                "--deck-file",
                "DECKFILE",
                "--device-param",
                "bogus",
                "--extract-only",
            ],
            "not CLASS:PARAM=NAME",
        ),
    ],
)
def test_cli_runset_flag_usage_errors_exit_2(
    tmp_path, fake_klayout, capsys, argv, needle
):
    code, out, err = _cli(tmp_path, *argv, capsys=capsys)

    assert code == 2
    assert out == ""
    assert needle in err["error"]["message"]
    assert err["error"]["command"] == "pex"
    assert "cmd" not in fake_klayout


def test_cli_default_engine_still_requires_a_deck(tmp_path, fake_klayout, capsys):
    code, _out, err = _cli(tmp_path, "--extract-only", capsys=capsys)

    assert code == 2
    assert "--deck" in err["error"]["message"]


def test_cli_curated_deck_is_refused_by_name_under_the_runset_engine(
    tmp_path, fake_klayout, capsys
):
    code, _out, err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--deck",
        "sky130",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 1
    assert "cannot honour --deck" in err["error"]["message"]


def test_cli_parasitic_only_flag_is_refused_by_name_under_the_runset_engine(
    tmp_path, fake_klayout, capsys
):
    code, _out, err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--critical-net",
        "n1",
        "--distributed-rc",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 1
    message = err["error"]["message"]
    assert "--critical-net" in message
    assert "--distributed-rc" in message


def test_cli_dry_run_is_refused_under_the_runset_engine(tmp_path, fake_klayout, capsys):
    """`--dry-run`'s preflight models the curated deck and parasitics; it does
    not pretend to model an engine it does not."""
    code, _out, err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--dry-run",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 1
    assert "--dry-run does not model --engine klayout-runset" in err["error"]["message"]
    assert "cmd" not in fake_klayout


def test_runset_from_args_is_none_for_the_default_engine():
    assert runset_from_args("klayout", None, None, None, None) is None


def test_submit_manifest_records_the_runset_arguments_with_an_absolute_deck_file(
    tmp_path, monkeypatch
):
    """`--attach` rebuilds the request from the manifest, so the engine and the
    runset path (made absolute) have to be recorded -- a bare `--attach` from
    another working directory must resolve the runset identically."""
    monkeypatch.chdir(tmp_path)
    args = create_parser().parse_args(
        [
            "pex",
            "l.gds",
            "tb.json",
            "--engine",
            "klayout-runset",
            "--deck-file",
            "pdk.lvs",
            "--deck-var",
            "run_mode=flat",
            "--device-param",
            "NMOS:W=w:u",
            "--runset-timeout",
            "30",
            "--submit-only",
            "--backend",
            "batch",
        ]
    )

    request = pex_cmd._submit_request(args)

    assert request["engine"] == "klayout-runset"
    assert request["deck_file"] == str(tmp_path / "pdk.lvs")
    assert request["deck_var"] == ["run_mode=flat"]
    assert request["device_params"] == ["NMOS:W=w:u"]
    assert request["runset_timeout"] == 30.0


# --------------------------------------------------------------------------- #
# `klt extract`'s own structured disclosure, which pex copies
# --------------------------------------------------------------------------- #


def test_extract_report_carries_params_not_carried(tmp_path, fake_klayout):
    assert _run(tmp_path)["params_not_carried"] == {"NMOS": NMOS_PARAMS}


def test_extract_report_params_not_carried_is_empty_when_all_declared(
    tmp_path, fake_klayout
):
    report = _run(tmp_path, device_params={"NMOS": ALL_PARAMS})

    assert report["params_not_carried"] == {}


# --------------------------------------------------------------------------- #
# `klt signoff` surfaces the disclosure like `body_bias` -- reported, not graded
# --------------------------------------------------------------------------- #

PEX_RUNSET_PASS_ENVELOPE = {
    **PEX_PASS_ENVELOPE,
    "extraction": {
        "engine": "klayout-runset",
        "parasitics_carried": False,
        "params_not_carried": {"pnpMPA": ["A", "P"]},
        "device_counts": {"pnpMPA": 10},
    },
    "body_bias": {
        "status": "not_assessed",
        "unbiased_device_count": None,
        "unbiased_nets": None,
        "unbiased_pmos_body_nets": None,
    },
}

DISCLOSURE = {
    "engine": "klayout-runset",
    "parasitics_carried": False,
    "params_not_carried": {"pnpMPA": ["A", "P"]},
}


def test_signoff_detail_surfaces_the_runset_engine_disclosure(tmp_path):
    path = _write(tmp_path, "pex.json", PEX_RUNSET_PASS_ENVELOPE)

    check = build_signoff([path])["checks"][0]

    assert check["kind"] == "pex"
    assert check["detail"]["pex_extraction"] == DISCLOSURE
    # Not-assessed is quoted as such, never as a clean `biased`.
    assert check["detail"]["body_bias"]["status"] == "not_assessed"
    assert check["detail"]["body_bias"]["unbiased_nets"] == []


def test_signoff_citation_surfaces_the_runset_engine_disclosure(tmp_path):
    path = _write(tmp_path, "pex.json", PEX_RUNSET_PASS_ENVELOPE)

    report = build_tier_report(_manifest(evidence={"7": path}))

    item_7 = next(item for item in report["items"] if item["id"] == 7)
    assert item_7["citation"]["pex_extraction"] == DISCLOSURE


def test_signoff_does_not_grade_on_the_runset_disclosure(tmp_path):
    """Reported, never graded: grading policy for a parasitic-free leg is
    issue #6606's. The verdict is exactly the curated envelope's."""
    runset = _write(tmp_path, "runset.json", PEX_RUNSET_PASS_ENVELOPE)
    curated = _write(tmp_path, "curated.json", PEX_PASS_ENVELOPE)

    runset_result = build_signoff([runset])
    curated_result = build_signoff([curated])

    assert runset_result["status"] == curated_result["status"] == "pass"
    assert runset_result["checks"][0]["passed"] is True


def test_signoff_omits_the_disclosure_for_a_curated_pex_envelope(tmp_path):
    path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    check = build_signoff([path])["checks"][0]
    report = build_tier_report(_manifest(evidence={"7": path}))
    item_7 = next(item for item in report["items"] if item["id"] == 7)

    assert "pex_extraction" not in check["detail"]
    assert "pex_extraction" not in (item_7["citation"] or {})


# --------------------------------------------------------------------------- #
# Real PDK: one local `klt pex --extract-only`, no grid
# --------------------------------------------------------------------------- #


def _pnpmpa_inputs():
    from klayout_tools import pdk

    try:
        root = pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["klayout"]
    except Exception:  # any resolution failure means "absent"
        pytest.skip("no ihp-sg13cmos5l PDK install")
    lvs_dir = os.path.join(root, "tech", "lvs")
    case = os.path.join(lvs_dir, "testing", "testcases", "unit", "bjt_devices")
    paths = (
        os.path.join(lvs_dir, "sg13cmos5l.lvs"),
        os.path.join(case, "layout", "pnpMPA.gds"),
    )
    if not all(os.path.isfile(p) for p in paths):
        pytest.skip("ihp-sg13cmos5l install lacks the pnpMPA LVS testcase")
    return paths


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_real_runset_pex_extract_only_sees_pnpmpa(tmp_path):
    """The gap this route closes: the curated deck sees no `pnpMPA` here."""
    deck, gds = _pnpmpa_inputs()

    report = run_pex(
        gds,
        [],
        None,
        top="pnpMPA_lvs",
        output=str(tmp_path / "pnpMPA.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
        runset=PexRunset(deck_file=deck),
    )

    extraction = report["extraction"]
    assert extraction["engine"] == "klayout-runset"
    assert extraction["parasitics_carried"] is False
    assert extraction["device_counts"].get("pnpMPA", 0) > 0
