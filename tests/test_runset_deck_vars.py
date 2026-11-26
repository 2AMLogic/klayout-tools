"""The ``klayout-runset`` engines share one ``deck_vars`` validator (issue
#7952).

``klt lvs`` (``lvs_runset._parse_deck_vars``) and ``klt extract`` / ``klt pex``
(``extract_runset_flags.parse_deck_vars``) each used to carry its own copy of
the normalisation loop and of the five engine-owned ``-rd`` names, and ``klt
pdk decks`` reads the LVS copy to hide those names from a runset's listed
globals. Every case below runs the same input through both callers, so the
shared policy is proved at each verb's own entry point -- and each verb still
raises its own error class, with the wording its callers already parse.

The engine fixtures come from ``tests/test_extract_runset.py`` and
``tests/helpers/lvs_runset_fixtures.py``; the command-path cases live here
because both verbs' own runset test modules sit just under the file-size
limit.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest

from helpers.lvs_runset_fixtures import request as _request
from klayout_tools import extract_runset, extract_runset_flags, lvs_runset, pex_runset
from klayout_tools.extract import run_extract
from klayout_tools.extract_spef import ExtractError
from klayout_tools.lvs import LvsError, run_lvs
from klayout_tools.runset_deck_vars import RESERVED_RUNSET_VARS, normalize_deck_vars
from test_extract_runset import _inputs
from test_extract_runset import fake_klayout as fake_klayout


class Caller(NamedTuple):
    parse: Callable[[Any], dict[str, str]]
    error_cls: type[Exception]
    not_object: str
    reserved: str
    not_scalar: str


#: Both callers, each with the three diagnostics it raises, verbatim.
CALLERS = [
    pytest.param(
        Caller(
            lvs_runset._parse_deck_vars,
            LvsError,
            "request.deck_vars must be a JSON object of name -> value",
            "deck_vars may not set '{name}': the klayout-runset engine sets "
            "input, schematic, report, top, topcell itself",
            "deck_vars['{name}'] must be a string, number or boolean",
        ),
        id="lvs",
    ),
    pytest.param(
        Caller(
            extract_runset_flags.parse_deck_vars,
            ExtractError,
            "deck_vars must be a JSON object of name -> value",
            "--deck-var may not set '{name}': the klayout-runset engine sets "
            "input, schematic, report, top, topcell itself",
            "deck_vars['{name}'] must be a string, number or bool",
        ),
        id="extract",
    ),
]

RESERVED_NAMES = ["input", "schematic", "report", "top", "topcell"]


def test_reserved_names_are_one_definition_every_reader_shares():
    assert RESERVED_RUNSET_VARS == ("input", "schematic", "report", "top", "topcell")
    assert lvs_runset.RESERVED_RUNSET_VARS is RESERVED_RUNSET_VARS
    assert extract_runset_flags.RESERVED_RUNSET_VARS is RESERVED_RUNSET_VARS
    assert extract_runset.RESERVED_RUNSET_VARS is RESERVED_RUNSET_VARS


@pytest.mark.parametrize("caller", CALLERS)
def test_scalars_are_stringified(caller):
    assert caller.parse(
        {"on": True, "off": False, "n": 3, "x": 2.5, "s": "flat", "": "e"}
    ) == {
        "on": "true",
        "off": "false",
        "n": "3",
        "x": "2.5",
        "s": "flat",
        "": "e",
    }


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("raw", [None, {}, [], (), "", 0, False])
def test_falsey_top_level_input_means_no_variables(caller, raw):
    assert caller.parse(raw) == {}


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("name", RESERVED_NAMES)
def test_reserved_name_is_refused_in_the_verbs_own_error(caller, name):
    with pytest.raises(caller.error_cls) as exc:
        caller.parse({name: "x"})
    assert str(exc.value) == caller.reserved.format(name=name)


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("value", [None, {"nested": 1}, [1], [], {}])
def test_malformed_value_names_the_variable(caller, value):
    with pytest.raises(caller.error_cls) as exc:
        caller.parse({"a": value})
    assert str(exc.value) == caller.not_scalar.format(name="a")


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("raw", [["FEOL=true"], [1], "FEOL=true", 3, ("a",)])
def test_non_object_top_level_is_refused(caller, raw):
    with pytest.raises(caller.error_cls) as exc:
        caller.parse(raw)
    assert str(exc.value) == caller.not_object


def test_shared_validator_raises_the_callers_error_class_from_its_templates():
    class Boom(Exception):
        pass

    def attempt(raw: Any) -> str:
        with pytest.raises(Boom) as exc:
            normalize_deck_vars(
                raw,
                error_cls=Boom,
                not_object="give an object",
                reserved="nope: {name} (reserved: {reserved})",
                not_scalar="bad {name}",
            )
        return str(exc.value)

    assert attempt([1]) == "give an object"
    assert attempt({"top": 1}) == (
        "nope: top (reserved: input, schematic, report, top, topcell)"
    )
    assert attempt({"a": None}) == "bad a"


@pytest.mark.parametrize(
    ("deck_vars", "needle"),
    [
        ({"input": "x"}, "deck_vars may not set 'input'"),
        ({"a": None}, "deck_vars['a'] must be a string, number or boolean"),
        ({"a": {"nested": "x"}}, "deck_vars['a'] must be a string, number or boolean"),
        (["FEOL=true"], "request.deck_vars must be a JSON object of name -> value"),
    ],
)
def test_lvs_command_path_refuses_bad_deck_vars(
    tmp_path, fake_klayout, deck_vars, needle
):
    req = json.loads(_request(tmp_path, 1))
    req["deck_vars"] = deck_vars
    with pytest.raises(LvsError) as exc:
        run_lvs(json.dumps(req))
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    ("deck_vars", "needle"),
    [
        ({"topcell": "OTHER"}, "--deck-var may not set 'topcell'"),
        ({"a": None}, "deck_vars['a'] must be a string, number or bool"),
        ({"a": [1]}, "deck_vars['a'] must be a string, number or bool"),
    ],
)
def test_extract_command_path_refuses_bad_deck_vars(
    tmp_path, fake_klayout, deck_vars, needle
):
    gds, deck = _inputs(tmp_path)
    with pytest.raises(ExtractError) as exc:
        run_extract(
            gds,
            None,
            engine="klayout-runset",
            deck_file=deck,
            deck_vars=deck_vars,
        )
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    ("deck_vars", "needle"),
    [
        ({"input": "x"}, "--deck-var may not set 'input'"),
        ({"a": None}, "deck_vars['a'] must be a string, number or bool"),
        ({"a": {"nested": "x"}}, "deck_vars['a'] must be a string, number or bool"),
    ],
)
def test_pex_runset_args_refuse_bad_deck_vars(deck_vars, needle):
    with pytest.raises(ExtractError) as exc:
        pex_runset.runset_from_args("klayout-runset", "pdk.lvs", deck_vars, None, None)
    assert needle in str(exc.value)
