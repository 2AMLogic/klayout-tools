"""Tests for `scripts/check_native_extras.py` (issue #2900): the pre-publish
guard that refuses a release whose `klt-*-native` extra pin is not on PyPI."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_native_extras", REPO_ROOT / "scripts" / "check_native_extras.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _pyproject(*reqs: str, extra: str = "yield") -> dict:
    return {
        "project": {"optional-dependencies": {extra: list(reqs), "dev": ["pytest"]}}
    }


def test_resolvable_pin_passes():
    seen = []

    def fetch(url):
        seen.append(url)
        return 200

    assert guard.check(_pyproject("klt-yield-native>=0.1.0,<0.2"), fetch) == []
    assert seen == ["https://pypi.org/pypi/klt-yield-native/0.1.0/json"]


def test_unpublished_pin_fails_naming_docs():
    problems = guard.check(_pyproject("klt-yield-native>=0.1.0,<0.2"), lambda u: 404)
    assert [s for s, _ in problems] == [guard.MISSING]
    assert "docs/releasing-native.md" in problems[0][1]
    assert "not published" in problems[0][1]


def test_pypi_outage_fails_closed_with_distinct_state():
    for status in (500, 503, 0):
        problems = guard.check(
            _pyproject("klt-yield-native>=0.1.0"), lambda u, s=status: s
        )
        assert [s for s, _ in problems] == [guard.UNREACHABLE]
        assert "not published" not in problems[0][1]


def test_no_native_pins_passes_without_fetching():
    def fetch(url):
        raise AssertionError("must not query PyPI")

    assert guard.check(_pyproject("numpy>=1", "klayout"), fetch) == []
    assert guard.check({}, fetch) == []


def test_extras_marker_and_equality_forms_parse():
    pins = guard.native_pins(
        _pyproject(
            "klt-mom-native[x]==0.3.1 ; python_version>='3.10'", "klt-yield-native"
        )
    )
    assert pins == [
        ("yield", "klt-mom-native", "0.3.1"),
        ("yield", "klt-yield-native", ""),
    ]


def test_this_repos_pyproject_has_a_yield_pin():
    import re

    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r'"klt-yield-native>=', text)


def test_publish_workflow_runs_the_guard_before_publishing():
    text = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "scripts/check_native_extras.py" in text
    assert text.index("check_native_extras.py") < text.index("uv publish")
