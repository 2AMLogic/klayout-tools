"""Ratchet: every post-run ``*_unsupported_worker`` echo check maps to a
runner-feature registry row (issue #5645).

Each of the five fields this issue registered carried a post-run echo check
with no ``REQUEST_FEATURE_MIN_RUNNER_VERSION`` row, so an old runner image
ran the whole grid and the result was then refused wholesale -- with
``--dry-run`` reporting ``status: valid`` the whole time, because the gate
had nothing to compare. This guard enumerates every
``*UNSUPPORTED_WORKER_CODE`` constant under ``src/klayout_tools/`` and fails
when a new one lands without a registry row (or an explicit waiver below), so
the sixth such field cannot repeat the failure mode.
"""

import ast
import re
from pathlib import Path

import pytest

import klayout_tools.sim_batch as sb

_SRC = Path(__file__).resolve().parents[1] / "src" / "klayout_tools"
_CODE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*UNSUPPORTED_WORKER_CODE$")

#: Echo-check constant -> the registry feature its field must map to. Every
#: mapped feature must also be mandatory: its echo check hard-fails the
#: whole request after the grid ran, so a warn-only registry row would keep
#: exactly the failure mode this guard exists to prevent.
_CODE_TO_FEATURE = {
    "PARAMS_UNSUPPORTED_WORKER_CODE": "corners.params",
    "OPTIONS_UNSUPPORTED_WORKER_CODE": "corners.options",
    "NODESET_UNSUPPORTED_WORKER_CODE": "options.nodeset",
    "IC_UNSUPPORTED_WORKER_CODE": "options.initial_conditions",
    "AO_UNSUPPORTED_WORKER_CODE": "analysis_overrides",
    "OP_CHECK_UNSUPPORTED_WORKER_CODE": "analysis.op_check",
    "SUPPLY_PRECISION_UNSUPPORTED_WORKER_CODE": (
        "lossless supply_v corner_id spelling"
    ),
}

#: Echo-check constants that deliberately carry no registry row, each with a
#: one-line reason (e.g. a field that predates the oldest fleet image and so
#: needs no version gate). Empty today.
_WAIVED: dict[str, str] = {}


def _echo_codes() -> dict[str, str]:
    """Every module-level ``<NAME> = "<code>"`` assignment under
    ``src/klayout_tools/`` whose name ends in ``UNSUPPORTED_WORKER_CODE``:
    ``{constant name: code value}``."""
    found: dict[str, str] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if not _CODE_NAME_RE.match(target.id):
                continue
            assert isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ), f"{path.name}: {target.id} must be a plain string constant"
            found[target.id] = node.value.value
    return found


def _assert_covered(codes: dict[str, str]) -> None:
    registered = {key.split(" (")[0] for key in sb.REQUEST_FEATURE_MIN_RUNNER_VERSION}
    for name, value in sorted(codes.items()):
        if name in _WAIVED:
            continue
        feature = _CODE_TO_FEATURE.get(name)
        assert feature is not None, (
            f"{name} (= {value!r}) has no REQUEST_FEATURE_MIN_RUNNER_VERSION "
            "row: add one (with a _RELEASED_FEATURE_DETECTORS detector and a "
            "_MANDATORY_RUNNER_FEATURES entry), or record a waiver in "
            "_WAIVED here with a one-line reason"
        )
        assert feature in registered, (
            f"{name} maps to {feature!r}, which is not a released "
            "REQUEST_FEATURE_MIN_RUNNER_VERSION row"
        )
        assert feature in sb._MANDATORY_RUNNER_FEATURES, (
            f"{feature!r} must be mandatory: its echo check {value!r} "
            "hard-fails the request after the grid ran"
        )
    stale = sorted(set(_CODE_TO_FEATURE) - set(codes))
    assert not stale, f"stale mapping (constant no longer defined): {stale}"


def test_every_unsupported_worker_code_has_a_registry_row_or_waiver():
    _assert_covered(_echo_codes())


def test_the_ratchet_bites_on_an_unregistered_echo_code():
    """A synthetic echo constant with no row fails the guard, so adding the
    sixth field without registering it cannot pass silently."""
    injected = dict(_echo_codes())
    injected["WEIRD_UNSUPPORTED_WORKER_CODE"] = "weird_unsupported_worker"
    with pytest.raises(AssertionError, match="WEIRD_UNSUPPORTED_WORKER"):
        _assert_covered(injected)


def test_a_waived_echo_code_passes_only_through_the_waiver():
    """The waiver is the only other exit: a synthetic constant recorded in
    ``_WAIVED`` is accepted without a registry row."""
    waived = dict(_echo_codes())
    waived["ANCIENT_UNSUPPORTED_WORKER_CODE"] = "ancient_unsupported_worker"
    _WAIVED["ANCIENT_UNSUPPORTED_WORKER_CODE"] = (
        "predates the oldest fleet image; no version gate needed"
    )
    try:
        _assert_covered(waived)
    finally:
        del _WAIVED["ANCIENT_UNSUPPORTED_WORKER_CODE"]
