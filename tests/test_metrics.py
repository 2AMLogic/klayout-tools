"""Tests for the declared metric namespace registry (`klayout_tools.metrics`,
issue #247).

Covers the registry mechanics themselves (naming grammar, duplicate
detection, `aggregate()` dispatch) and a schema test asserting every
registered entry carries a valid `{aggregator, higher_is_better, critical}`
tuple -- the acceptance-criteria bar from issue #247's curator enhancement.
"""

from __future__ import annotations

import pytest

from klayout_tools import metrics as metrics_module
from klayout_tools.metrics import (
    REGISTRY,
    MetricNamespaceError,
    aggregate,
    all_metrics,
    get_metric,
    is_registered,
    register,
)

# --- registry schema (every entry is valid) --------------------------------


def test_registry_is_non_empty():
    """The pilot (`klt layout-metrics`, issue #247) registers at least its
    own four declared metrics."""
    assert len(REGISTRY) >= 4


@pytest.mark.parametrize("metric_def", all_metrics(), ids=lambda m: m.name)
def test_every_registered_metric_has_valid_tuple(metric_def):
    assert metric_def.aggregator in ("sum", "min", "max", "mean")
    assert metric_def.higher_is_better in (True, False, None)
    assert isinstance(metric_def.critical, bool)
    assert metric_def.name  # non-empty
    # Naming grammar: lowercase, double-underscore-separated segments.
    segments = metric_def.name.split("__")
    assert len(segments) >= 2
    assert all(segment and segment.islower() for segment in segments)


def test_pilot_metrics_are_registered():
    for name in (
        "design__layer__count",
        "design__cell__count",
        "design__instance__count",
        "drc__error__count",
    ):
        assert is_registered(name)


def test_drc_error_count_is_critical_and_lower_is_better():
    metric_def = get_metric("drc__error__count")
    assert metric_def.critical is True
    assert metric_def.higher_is_better is False


def test_structural_count_metrics_have_no_declared_polarity():
    """Structural counts (layer/cell/instance) are not, by themselves, a
    quality axis -- `higher_is_better` is `None`, not fabricated."""
    for name in (
        "design__layer__count",
        "design__cell__count",
        "design__instance__count",
    ):
        assert get_metric(name).higher_is_better is None


# --- get_metric / is_registered ---------------------------------------------


def test_get_metric_raises_on_unknown_name():
    with pytest.raises(MetricNamespaceError, match="unknown metric"):
        get_metric("not__a__real__metric")


def test_is_registered_false_for_unknown_name():
    assert is_registered("not__a__real__metric") is False


# --- register() validation --------------------------------------------------


def test_register_rejects_bad_name_grammar():
    with pytest.raises(MetricNamespaceError, match="naming grammar|must be lowercase"):
        register("NotValid", aggregator="sum", higher_is_better=None)


def test_register_rejects_single_segment_name():
    with pytest.raises(MetricNamespaceError):
        register("nosegments", aggregator="sum", higher_is_better=None)


def test_register_rejects_unknown_aggregator():
    with pytest.raises(MetricNamespaceError, match="unknown aggregator"):
        register("test__bad__aggregator", aggregator="median", higher_is_better=None)  # type: ignore[arg-type]


def test_register_rejects_duplicate_name(monkeypatch):
    # Isolate from the real module-level registry so this test can't leak
    # state into other tests (or collide with a real pilot name).
    monkeypatch.setattr(metrics_module, "REGISTRY", dict(REGISTRY))
    register("test__duplicate__count", aggregator="sum", higher_is_better=None)
    with pytest.raises(MetricNamespaceError, match="already registered"):
        register("test__duplicate__count", aggregator="sum", higher_is_better=None)


def test_register_returns_the_metric_def(monkeypatch):
    monkeypatch.setattr(metrics_module, "REGISTRY", dict(REGISTRY))
    metric_def = register(
        "test__example__count",
        aggregator="max",
        higher_is_better=True,
        critical=True,
        description="an example",
    )
    assert metric_def.name == "test__example__count"
    assert metric_def.aggregator == "max"
    assert metric_def.higher_is_better is True
    assert metric_def.critical is True
    assert metric_def.description == "an example"
    assert is_registered("test__example__count")


# --- aggregate() -------------------------------------------------------------


def test_aggregate_sum():
    assert aggregate("design__instance__count", [3, 4, 5]) == 12


def test_aggregate_max():
    assert aggregate("design__layer__count", [3, 7, 5]) == 7


def test_aggregate_min(monkeypatch):
    monkeypatch.setattr(metrics_module, "REGISTRY", dict(REGISTRY))
    register("test__min__metric", aggregator="min", higher_is_better=None)
    assert aggregate("test__min__metric", [3, 7, 5]) == 3


def test_aggregate_mean(monkeypatch):
    monkeypatch.setattr(metrics_module, "REGISTRY", dict(REGISTRY))
    register("test__mean__metric", aggregator="mean", higher_is_better=None)
    assert aggregate("test__mean__metric", [2, 4, 6]) == 4


def test_aggregate_raises_on_empty_values():
    with pytest.raises(ValueError, match="non-empty"):
        aggregate("design__instance__count", [])


def test_aggregate_raises_on_unknown_metric():
    with pytest.raises(MetricNamespaceError):
        aggregate("not__a__real__metric", [1, 2, 3])
