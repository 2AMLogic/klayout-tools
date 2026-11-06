"""Install-hint and provenance for ``klt_yield_native`` (issue #5018).

Needs no built extension: the import and the distribution lookup are patched.
"""

from __future__ import annotations

import importlib
import importlib.metadata

import pytest

from klayout_tools import yield_analysis, yield_sensitivity
from klayout_tools._native import _load_native_extension


def _no_import(name: str, *args, **kwargs):
    raise ImportError(name)


def test_yield_error_names_extra_and_checkout_recipe(monkeypatch):
    monkeypatch.setattr(importlib, "import_module", _no_import)
    with pytest.raises(yield_analysis.YieldError) as err:
        yield_analysis._load_native()
    msg = str(err.value)
    assert "klayout-tools[yield]" in msg
    assert "uvx --from" in msg
    assert "maturin develop --release" in msg
    assert "uv sync --group yield" in msg


def test_sensitivity_error_names_extra(monkeypatch):
    monkeypatch.setattr(importlib, "import_module", _no_import)
    with pytest.raises(yield_sensitivity.SensitivityError) as err:
        yield_sensitivity._load_native()
    assert "klayout-tools[yield]" in str(err.value)


def test_callers_without_extra_keep_original_message(monkeypatch):
    monkeypatch.setattr(importlib, "import_module", _no_import)
    with pytest.raises(RuntimeError) as err:
        _load_native_extension(
            "klt_mom_native", RuntimeError, "native/mom/", "pip install ./native/mom"
        )
    assert str(err.value) == (
        "the klt_mom_native extension is not installed -- from a repo "
        "checkout, run `maturin develop --release` inside native/mom/ "
        "(or `pip install ./native/mom`)"
    )


def test_native_provenance_version(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    assert yield_analysis._native_provenance() == {
        "module": "klt_yield_native",
        "version": "0.1.0",
    }


def test_native_provenance_null_without_metadata(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert yield_analysis._native_provenance() == {
        "module": "klt_yield_native",
        "version": None,
    }
