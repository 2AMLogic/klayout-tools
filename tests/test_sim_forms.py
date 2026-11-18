"""Tests for the measurement-form registry in ``sim_forms`` (issue #5695).

The exclusive form modules used to each carry a hand-maintained tuple of
"every *other* exclusive form"; those tuples are now derived from one ordered
registry. These tests are the assertion those per-module string literals stood in for:
that the exclusivity matrix is complete, and that it stays complete when a new
form module is added.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

from klayout_tools import sim, sim_forms, sim_validate
from klayout_tools.cli import main
from klayout_tools.sim_errors import SimError

#: A minimal body per form. Exclusivity is checked before any body validator
#: runs, so these only have to be non-``None``.
_BODY = {form: {"placeholder": 1} for form in sim_forms.FORMS}
_BODY["spice"] = ".meas tran x find v(a) at=1e-9"
_BODY["expr"] = "v(a)"


def _declare(*forms: str) -> dict[str, object]:
    return {"name": "m", **{form: _BODY[form] for form in forms}}


_MALFORMED_MEASUREMENTS = [
    (None, "request.measurements", "NoneType"),
    ("notalist", "request.measurements", "str"),
    ({}, "request.measurements", "dict"),
    (1, "request.measurements", "int"),
    (1.5, "request.measurements", "float"),
    (False, "request.measurements", "bool"),
    (["notadict"], "measurements[0]", "str"),
    ([None], "measurements[0]", "NoneType"),
    ([1], "measurements[0]", "int"),
    ([True], "measurements[0]", "bool"),
    ([[]], "measurements[0]", "list"),
    ([_declare("expr"), None], "measurements[1]", "NoneType"),
]


@pytest.mark.parametrize("measurements,path,type_name", _MALFORMED_MEASUREMENTS)
def test_malformed_measurements_raise_sim_error(measurements, path, type_name):
    """Present null is invalid input, rather than an alias for an absent key."""
    request = {"analysis": {"kind": "tran", "args": "1u 2m"}}
    request["measurements"] = measurements
    with pytest.raises(SimError) as excinfo:
        sim._resolve_analysis_and_measurements(request)
    assert path in str(excinfo.value)
    assert type_name in str(excinfo.value)


@pytest.mark.parametrize("measurements,path,type_name", _MALFORMED_MEASUREMENTS)
def test_malformed_measurements_cli_json_error(
    tmp_path, capsys, measurements, path, type_name
):
    netlist = tmp_path / "x.sp"
    netlist.write_text("* minimal circuit\nV1 out 0 1\nR1 out 0 1k\n.end\n")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": str(netlist),
                "analysis": {"kind": "tran", "args": "1u 2m"},
                "measurements": measurements,
            }
        )
    )
    assert main(["sim", str(request), "--dry-run", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    envelope = json.loads(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "sim"
    assert path in envelope["error"]["message"]
    assert type_name in envelope["error"]["message"]


@pytest.mark.parametrize(
    "fields", [{}, {"measurements": []}, {"measurements": [_declare("expr")]}]
)
def test_valid_measurements_preserve_resolution(fields):
    analysis = {"kind": "tran", "args": "1u 2m"}
    resolved_analysis, measurements = sim._resolve_analysis_and_measurements(
        {"analysis": analysis, **fields}
    )
    assert resolved_analysis == analysis
    assert measurements == fields.get("measurements", [])


def test_registry_covers_exactly_the_registered_exclusive_validators():
    """One registered validator per registered exclusive form, and no form
    without one -- each validator's own module is identified by the single
    ``is_<form>`` predicate it defines, so this needs no second form list."""
    owned = []
    for validator in sim_validate._EXCLUSIVE_FORM_VALIDATORS:
        module = sys.modules[validator.__module__]
        forms = [
            form for form in sim_forms.EXCLUSIVE_FORMS if hasattr(module, f"is_{form}")
        ]
        assert len(forms) == 1, f"{module.__name__} owns {forms}"
        owned.append(forms[0])
    assert sorted(owned) == sorted(sim_forms.EXCLUSIVE_FORMS)


def test_other_forms_omits_only_the_named_form_and_keeps_order():
    for form in sim_forms.FORMS:
        others = sim_forms.other_forms(form)
        assert form not in others
        assert set(others) == set(sim_forms.FORMS) - {form}
        # Declaration order is observable: the refusal names ``others[0]``.
        assert others == tuple(f for f in sim_forms.FORMS if f != form)


def test_other_forms_refuses_an_unregistered_form():
    """A new form module that forgets the registry fails loudly, not quietly."""
    with pytest.raises(ValueError, match="unknown measurement form"):
        sim_forms.other_forms("not_a_form")


def test_measurements_request_row_does_not_list_forms():
    doc = Path(__file__).resolve().parents[1] / "docs" / "cli" / "sim.md"
    request = doc.read_text(encoding="utf-8").split("\n### Request\n", 1)[1]
    request = request.split("\n### Response\n", 1)[0]
    row = next(
        line for line in request.splitlines() if line.startswith("| `measurements[]` ")
    )

    # These two names are still used in form-specific field restrictions.
    mentioned_forms = {form for form in sim_forms.FORMS if f"`{form}`" in row}
    assert mentioned_forms <= {"spice", "expr"}


@pytest.mark.parametrize(
    "first,second", list(itertools.permutations(sim_forms.EXCLUSIVE_FORMS, 2))
)
def test_every_exclusive_pair_is_refused_in_both_orders(first, second):
    with pytest.raises(SimError, match="alternative forms") as excinfo:
        sim_validate._validate_measurement_forms([_declare(first, second)])
    message = str(excinfo.value)
    assert f"'{first}'" in message and f"'{second}'" in message


@pytest.mark.parametrize(
    "exclusive,other",
    list(itertools.product(sim_forms.EXCLUSIVE_FORMS, sim_forms.NON_EXCLUSIVE_FORMS)),
)
def test_every_exclusive_form_refuses_every_non_exclusive_form(exclusive, other):
    """Issue #5695: ``transfer`` omitted ``noise`` -- which postdates it -- so
    ``{"transfer": ..., "noise": ...}`` was accepted as a plain transfer entry
    and the noise request was silently dropped."""
    with pytest.raises(SimError, match="alternative forms") as excinfo:
        sim_validate._validate_measurement_forms([_declare(exclusive, other)])
    assert f"declares '{exclusive}' together with '{other}'" in str(excinfo.value)


@pytest.mark.parametrize("form", sim_forms.EXCLUSIVE_FORMS)
def test_an_entry_declaring_one_exclusive_form_needs_a_name(form):
    with pytest.raises(SimError, match="requires 'name'"):
        sim_validate._validate_measurement_forms([{form: _BODY[form]}])


def test_normalize_selector_is_shared_by_transfer_and_snm():
    from klayout_tools import sim_snm_meas, sim_transfer_meas

    assert sim_transfer_meas.normalize_selector is sim_forms.normalize_selector
    assert sim_snm_meas.normalize_selector is sim_forms.normalize_selector
    assert sim_forms.normalize_selector("V(OUT_A)") == "v(out_a)"
    assert sim_forms.normalize_selector(" v(a)") is None
    assert sim_forms.normalize_selector("i(v1)") is None
    assert sim_forms.normalize_selector(5) is None
