"""Tests for ``klt extract --engine klayout-runset --device-param`` (issue
#5513): carrying a device's measured KLayout parameters onto its ``X`` call.

Same faked-subprocess layer as ``tests/test_extract_runset.py`` (whose
``fake_klayout`` fixture, ``_run`` and ``_inputs`` this file reuses), kept in
its own file so neither carries file-size debt. The fake runset extracts
``NMOS`` devices, whose KLayout parameters are ``L W AS AD PS PD``.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import extract_runset
from klayout_tools.extract import run_extract
from klayout_tools.extract_spef import ExtractError
from test_extract_runset import _inputs, _run
from test_extract_runset import fake_klayout as fake_klayout

W_L = {"W": ("w", "u"), "L": ("l", "u")}


def _cards(report) -> list[str]:
    text = open(report["netlist_path"]).read()
    return [ln for ln in text.splitlines() if ln and not ln.startswith(("*", "."))]


def _call_params(card: str) -> dict[str, str]:
    """``{name: token}`` for the ``name=value`` words of one ``X`` card."""
    return dict(word.split("=", 1) for word in card.split() if "=" in word)


# --------------------------------------------------------------------------- #
# Carrying parameters
# --------------------------------------------------------------------------- #


def test_declared_parameters_ride_the_call_unscaled(tmp_path, fake_klayout):
    report = _run(tmp_path, device_params={"NMOS": W_L})
    (card,) = _cards(report)
    assert card.endswith(" NMOS l=0.2u w=0.4u")
    # Unscaled: the card's number is the report's own measured micron value,
    # and the suffix is the caller's, appended verbatim.
    params = report["devices"][0]["params"]
    carried = _call_params(card)
    assert float(carried["w"].removesuffix("u")) == pytest.approx(params["w_um"])
    assert float(carried["l"].removesuffix("u")) == pytest.approx(params["l_um"])


def test_a_declaration_without_a_suffix_appends_nothing(tmp_path, fake_klayout):
    report = _run(tmp_path, device_params={"NMOS": {"W": ("wtot", "")}})
    (card,) = _cards(report)
    assert _call_params(card) == {"wtot": "0.4"}


def test_junction_parameters_can_be_cross_declared(tmp_path, fake_klayout):
    """The MOS source/drain recipe from the issue: each measured area goes to
    the *other* name, because the call's S/D order is the extractor's."""
    cross = {
        **W_L,
        "AS": ("ad", "p"),
        "AD": ("as", "p"),
        "PS": ("pd", "u"),
        "PD": ("ps", "u"),
    }
    report = _run(tmp_path, device_params={"NMOS": cross})
    (card,) = _cards(report)
    assert set(_call_params(card)) == {"w", "l", "as", "ad", "ps", "pd"}
    assert report["warnings"] == []


def test_declarations_compose_with_a_device_model_rename(tmp_path, fake_klayout):
    report = _run(
        tmp_path,
        device_models={"NMOS": "pdk_nfet"},
        device_params={"NMOS": W_L},
    )
    (card,) = _cards(report)
    assert card.endswith(" pdk_nfet l=0.2u w=0.4u")
    assert "pdk_nfet" in report["warnings"][0]


def test_declaration_order_does_not_change_the_card(tmp_path, fake_klayout):
    a = _run(tmp_path, device_params={"NMOS": {"W": ("w", "u"), "L": ("l", "u")}})
    first = _cards(a)
    b = _run(tmp_path, device_params={"NMOS": {"L": ("l", "u"), "W": ("w", "u")}})
    assert _cards(b) == first


# --------------------------------------------------------------------------- #
# What is still disclosed
# --------------------------------------------------------------------------- #


def test_only_undeclared_parameters_are_named_in_warnings(tmp_path, fake_klayout):
    report = _run(tmp_path, device_params={"NMOS": W_L})
    (warning,) = report["warnings"]
    assert "carrying only the --device-param declarations for L, W" in warning
    assert "the measured AD, AS, PD, PS are not carried" in warning
    # A parameter that *was* carried is not claimed dropped.
    assert "measured L" not in warning and ", W," not in warning
    assert "with no parameters" not in warning
    text = open(report["netlist_path"]).read()
    assert "some device parameters are not carried" in text
    assert "no device parameters:" not in text


def test_fully_declared_class_has_no_dropped_parameter_warning(tmp_path, fake_klayout):
    everything = {
        "W": ("w", "u"),
        "L": ("l", "u"),
        "AS": ("as", "p"),
        "AD": ("ad", "p"),
        "PS": ("ps", "u"),
        "PD": ("pd", "u"),
    }
    report = _run(tmp_path, device_params={"NMOS": everything})
    assert report["warnings"] == []
    text = open(report["netlist_path"]).read()
    # Nothing is dropped, so the header must not claim otherwise.
    assert "no device parameters" not in text
    assert "not carried" not in text
    assert "no parasitics" in text


def test_a_declared_class_with_no_device_in_the_layout_warns(tmp_path, fake_klayout):
    report = _run(tmp_path, device_params={"PMOS": {"W": ("w", "u")}})
    (declared_warning, dropped_warning) = report["warnings"]
    assert "declares class 'PMOS'" in declared_warning
    assert "classes: NMOS" in declared_warning
    # The real class is untouched: still parameter-free, still disclosed.
    assert "with no parameters" in dropped_warning
    (card,) = _cards(report)
    assert _call_params(card) == {}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_unknown_parameter_is_an_error_and_writes_nothing(tmp_path, fake_klayout):
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path, device_params={"NMOS": {"W": ("w", "u"), "NF": ("nf", "")}})
    message = str(exc.value)
    assert "NF" in message and "'NMOS'" in message
    assert "defines: AD, AS, L, PD, PS, W" in message
    assert not (tmp_path / "out.spice").exists()


def test_parameter_names_are_case_sensitive(tmp_path, fake_klayout):
    with pytest.raises(ExtractError) as exc:
        _run(tmp_path, device_params={"NMOS": {"w": ("w", "u")}})
    assert "does not define" in str(exc.value)


def test_curated_engine_refuses_device_param(tmp_path):
    gds, _ = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(gds, "sky130", device_params={"NMOS": W_L})
    assert "device_params" in str(exc.value)
    assert "only used by engine 'klayout-runset'" in str(exc.value)


# --------------------------------------------------------------------------- #
# The default is unchanged
# --------------------------------------------------------------------------- #


def test_default_report_and_netlist_are_unchanged(tmp_path, fake_klayout):
    report = _run(tmp_path)
    # Present only when declared: a default report has no new key at all.
    assert "device_params" not in report
    (card,) = _cards(report)
    assert card.endswith(" NMOS") and "=" not in card
    (warning,) = report["warnings"]
    assert "with no parameters" in warning
    assert "the measured AD, AS, L, PD, PS, W are not carried" in warning
    assert "no device parameters:" in open(report["netlist_path"]).read()


def test_an_empty_declaration_is_the_default(tmp_path, fake_klayout):
    assert "device_params" not in _run(tmp_path, device_params={})


def test_declarations_are_echoed_sorted(tmp_path, fake_klayout):
    report = _run(tmp_path, device_params={"NMOS": {"W": ("w", "u"), "L": ("l", "")}})
    assert report["device_params"] == {
        "NMOS": {
            "L": {"name": "l", "suffix": ""},
            "W": {"name": "w", "suffix": "u"},
        }
    }
    assert list(report["device_params"]["NMOS"]) == ["L", "W"]


# --------------------------------------------------------------------------- #
# Parsing the flag
# --------------------------------------------------------------------------- #


def test_parse_device_params():
    parse = extract_runset.parse_device_params
    assert parse(None) is None
    assert parse([]) is None
    assert parse(["sg13_hv_nmos:W=w:u", "sg13_hv_nmos:L=l:u", "pnpMPA:A=a:p"]) == {
        "sg13_hv_nmos": {"W": ("w", "u"), "L": ("l", "u")},
        "pnpMPA": {"A": ("a", "p")},
    }
    # No suffix, and a suffix that is more than one letter.
    assert parse(["c:W=w"]) == {"c": {"W": ("w", "")}}
    assert parse(["c:W=w:meg"]) == {"c": {"W": ("w", "meg")}}
    # A later entry for the same CLASS:PARAM wins, like --device-model.
    assert parse(["c:W=w:u", "c:W=wtot:n"]) == {"c": {"W": ("wtot", "n")}}


@pytest.mark.parametrize(
    "entry",
    [
        "sg13_hv_nmos",  # no mapping at all
        "sg13_hv_nmos:W",  # no '='
        "sg13_hv_nmos=w:u",  # no PARAM
        ":W=w:u",  # blank CLASS
        "c:=w:u",  # blank PARAM
        "c:W=:u",  # blank NAME
        "c:W=1w:u",  # NAME not an identifier
        "c:W=w x:u",  # whitespace in NAME
        "c:W=w:u x",  # whitespace in SUFFIX
        "c:W=w:1e-6",  # SUFFIX is a scale letter, never an expression
    ],
)
def test_malformed_declaration_is_an_error(entry):
    with pytest.raises(ExtractError) as exc:
        extract_runset.parse_device_params([entry])
    assert "CLASS:PARAM=NAME[:SUFFIX]" in str(exc.value)
    assert repr(entry) in str(exc.value)


def test_two_parameters_cannot_target_one_name():
    with pytest.raises(ExtractError) as exc:
        extract_runset.parse_device_params(["c:AS=as:p", "c:AD=AS:p"])
    assert "same parameter name twice" in str(exc.value)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli(tmp_path, *extra):
    from klayout_tools.cli import main

    gds, deck = _inputs(tmp_path)
    return main(
        [
            "extract",
            gds,
            "--engine",
            "klayout-runset",
            "--deck-file",
            deck,
            "-o",
            str(tmp_path / "cli.spice"),
            "--format",
            "json",
            *extra,
        ]
    )


def test_cli_carries_a_declared_parameter(tmp_path, fake_klayout, capsys):
    rc = _cli(tmp_path, "--device-param", "NMOS:W=w:u", "--device-param", "NMOS:L=l:u")
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["device_params"]["NMOS"]) == {"W", "L"}
    (card,) = _cards(report)
    assert card.endswith(" NMOS l=0.2u w=0.4u")


def test_cli_unknown_parameter_is_a_usage_error(tmp_path, fake_klayout, capsys):
    rc = _cli(tmp_path, "--device-param", "NMOS:NF=nf")
    assert rc == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "NF" in message and "does not define" in message


def test_cli_malformed_declaration_is_a_usage_error(tmp_path, fake_klayout, capsys):
    rc = _cli(tmp_path, "--device-param", "NMOS-W-w")
    assert rc == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "--device-param entry 'NMOS-W-w'" in message


def test_cli_refuses_device_param_under_the_curated_engine(tmp_path, capsys):
    from klayout_tools.cli import main

    gds, _ = _inputs(tmp_path)
    rc = main(
        [
            "extract",
            gds,
            "--deck",
            "sky130",
            "--device-param",
            "NMOS:W=w:u",
            "--format",
            "json",
        ]
    )
    assert rc == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "only used by engine 'klayout-runset'" in message


def test_request_document_carries_device_params(tmp_path, fake_klayout, capsys):
    from klayout_tools.cli import main

    gds, deck = _inputs(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.extract.request/1",
                "file": gds,
                "engine": "klayout-runset",
                "deck_file": deck,
                "device_params": ["NMOS:W=w:u", "NMOS:L=l:u"],
                "output": str(tmp_path / "req.spice"),
            }
        )
    )
    assert main(["extract", str(request), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["device_params"]["NMOS"]) == {"W", "L"}
