"""Version-aware ngspice gating for simulator-dependent behavior."""

import subprocess
from unittest.mock import Mock

import pytest

from helpers import tool_probes as probes


@pytest.fixture
def version_probe(monkeypatch):
    runner = Mock()
    monkeypatch.setattr(probes, "NGSPICE_BINARY", "/tools/ngspice")
    monkeypatch.setattr(probes, "NGSPICE_TESTS_SKIPPED", False)
    monkeypatch.setattr(subprocess, "run", runner)
    probes.ngspice_major.cache_clear()
    yield runner
    probes.ngspice_major.cache_clear()


@pytest.mark.parametrize(
    ("banner", "major"),
    [
        ("******\n** ngspice-42 : Circuit level simulation program\n", 42),
        ("** ngspice-47 : Circuit level simulation program", 47),
        ("** ngspice-42.1+ds-3build1 : Circuit level simulation program", 42),
        ("unrecognized simulator version", None),
    ],
)
def test_ngspice_major_uses_the_banner_once(version_probe, banner, major):
    version_probe.return_value = subprocess.CompletedProcess([], 0, banner, "")
    assert probes.ngspice_major() == major
    assert probes.ngspice_major() == major
    version_probe.assert_called_once()


def test_minimum_major_names_installed_and_required_versions(version_probe):
    version_probe.return_value = subprocess.CompletedProcess([], 0, "ngspice-47", "")
    assert probes.requires_ngspice(min_major=46).mark.args == (False,)
    assert probes.requires_ngspice(min_major=47).mark.args == (False,)
    marker = probes.requires_ngspice(min_major=48).mark
    assert marker.args == (True,)
    assert "47" in marker.kwargs["reason"] and "48" in marker.kwargs["reason"]
    version_probe.assert_called_once()


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), subprocess.TimeoutExpired("ngspice", 60)]
)
def test_unavailable_version_skips_with_a_reason(version_probe, failure):
    version_probe.side_effect = failure
    marker = probes.requires_ngspice(min_major=42).mark
    assert marker.args == (True,)
    assert "unknown" in marker.kwargs["reason"] and "42" in marker.kwargs["reason"]


def test_missing_binary_skips_without_starting_a_probe(version_probe, monkeypatch):
    monkeypatch.setattr(probes, "NGSPICE_BINARY", None)
    assert probes.ngspice_major() is None
    marker = probes.requires_ngspice(min_major=47).mark
    assert marker.args == (True,)
    assert (
        "not installed" in marker.kwargs["reason"] and "47" in marker.kwargs["reason"]
    )
    version_probe.assert_not_called()


def test_opt_out_wins_without_starting_a_probe(version_probe, monkeypatch):
    monkeypatch.setattr(probes, "NGSPICE_TESTS_SKIPPED", True)
    marker = probes.requires_ngspice(min_major=42).mark
    assert marker.args == (True,)
    assert "KLT_SKIP_NGSPICE_TESTS" in marker.kwargs["reason"]
    version_probe.assert_not_called()
