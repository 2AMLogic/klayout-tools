"""Cross-field validation of ABC driving cells and cell exclusions."""

from unittest.mock import Mock

import pytest

from helpers.subprocess_fakes import _patch_yosys_run
from klayout_tools import synthesize
from klayout_tools.synthesize_driving_cell import first_matching_glob
from test_synthesize import (
    _append_liberty_cells,
    _setup_dont_use_env,
    _sky130_liberty_path,
    _stub_yosys_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

INV = "sky130_fd_sc_hd__inv_1"
ISOBUF = "sky130_fd_sc_hd__lpflow_isobufsrc_1"
LIBRARY_GLOB = "sky130_fd_sc_hd__lpflow_*"


def test_first_matching_glob_preserves_order_and_case():
    assert first_matching_glob("BUFx2_R", ("buf*", "*x2_*", "BUF*")) == "*x2_*"
    assert first_matching_glob("BUFx2_R", ("buf*",)) is None


@pytest.mark.parametrize(
    ("driver", "patterns", "matching_glob", "known_cells"),
    [
        (INV, [INV], INV, True),
        (INV, ["*__inv_*"], "*__inv_*", True),
        (ISOBUF, [], LIBRARY_GLOB, True),
        ("BUFx2_ASAP7_75t_R", ["*BUFx2_*"], "*BUFx2_*", False),
    ],
)
def test_rejects_excluded_driver_before_yosys(
    tmp_path, monkeypatch, driver, patterns, matching_glob, known_cells
):
    request = _setup_dont_use_env(
        tmp_path,
        monkeypatch,
        {"abc_constr": {"driving_cell": driver, "load_ff": 3}, "dont_use": patterns},
    )
    if not known_cells:
        monkeypatch.setattr(synthesize, "_liberty_cell_names", lambda _: None)
    _stub_yosys_success(monkeypatch, supports_dont_use=False)
    calls = Mock(wraps=synthesize.subprocess.run)
    _patch_yosys_run(monkeypatch, calls)

    with pytest.raises(synthesize.SynthesizeError) as exc:
        synthesize.run_synthesize(request)

    message = str(exc.value)
    assert f"abc_constr.driving_cell '{driver}'" in message
    assert f"pattern '{matching_glob}'" in message
    assert "dont_use_mode" in message
    assert all(call.args[0][0] != "yosys" for call in calls.call_args_list)
    assert not (tmp_path / ".klt").exists()


@pytest.mark.parametrize(
    ("mode", "patterns", "driver"),
    [
        ("replace", [INV], ISOBUF),
        ("none", [], ISOBUF),
        ("additive", ["*__o21a_*"], INV),
    ],
)
def test_allows_driver_outside_resolved_exclusions(
    tmp_path, monkeypatch, mode, patterns, driver
):
    request = _setup_dont_use_env(
        tmp_path,
        monkeypatch,
        {
            "abc_constr": {"driving_cell": driver, "load_ff": 3},
            "dont_use": patterns,
            "dont_use_mode": mode,
        },
    )
    _stub_yosys_success(monkeypatch)

    report = synthesize.run_synthesize(request)

    assert report["abc_constr"] == {
        "source": "request",
        "driving_cell": driver,
        "load_ff": 3,
    }
    library_globs = synthesize._ABC_DONT_USE_GLOBS["sky130_fd_sc_hd"]
    assert report["cell_exclusions"]["effective"] == (
        [*library_globs, *patterns] if mode == "additive" else patterns
    )


def test_exclusions_do_not_validate_the_builtin_driver(tmp_path, monkeypatch):
    driver, load = synthesize._ABC_CONSTR_INPUTS["sky130_fd_sc_hd"]
    request = _setup_dont_use_env(tmp_path, monkeypatch, {"dont_use": [driver]})
    _append_liberty_cells(_sky130_liberty_path(tmp_path), (driver,))
    _stub_yosys_success(monkeypatch)

    report = synthesize.run_synthesize(request)

    assert report["abc_constr"] == {
        "source": "builtin",
        "driving_cell": driver,
        "load_ff": load,
    }
    assert driver in report["cell_exclusions"]["effective"]
