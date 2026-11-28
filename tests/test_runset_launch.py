"""Caller-specific errors from the shared KLayout runset launcher."""

import subprocess

import pytest

from klayout_tools import runset_launch
from klayout_tools.extract_spef import ExtractError
from klayout_tools.lvs_base import LvsError


@pytest.mark.parametrize("error_cls", [LvsError, ExtractError])
def test_broken_binary_override_raises_callers_error(tmp_path, error_cls):
    with pytest.raises(error_cls) as exc:
        runset_launch.resolve_klayout_binary(
            {"klayout_binary": "./missing"}, str(tmp_path), error_cls=error_cls
        )
    assert str(exc.value).startswith("options.klayout_binary does not name a runnable")


@pytest.mark.parametrize(
    ("error_cls", "options", "expected_knob"),
    [
        (LvsError, {}, "options.runset_timeout_s"),
        (ExtractError, {"timeout_knob": "--runset-timeout"}, "--runset-timeout"),
    ],
)
def test_timeout_names_callers_knob(
    tmp_path, monkeypatch, error_cls, options, expected_knob
):
    def expire(cmd, *, timeout, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", expire)
    with pytest.raises(error_cls) as exc:
        runset_launch.run_runset(
            binary="klayout",
            deck_file="deck.lvs",
            layout="layout.gds",
            reference="ref.sp",
            top=None,
            deck_vars={},
            timeout_s=0.5,
            work_dir=str(tmp_path),
            error_cls=error_cls,
            **options,
        )
    assert str(exc.value) == (
        "the LVS runset did not complete within 0.5s "
        f"(raise {expected_knob} to allow more time)"
    )
