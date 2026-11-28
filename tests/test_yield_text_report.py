"""Golden regression for ``klt yield``'s text report renderer (issue #8110).

``yield_cmd.print_yield_report`` is shared with ``klt yield-campaign``, which
prints its own ``campaign`` header and then delegates the measurement body.
The CLI-level text tests in ``test_yield.py`` / ``test_yield_campaign.py``
need the ``klt_yield_native`` extension and skip without it, so this
byte-for-byte check runs everywhere and is the guard that the renderer's
layout does not move.

``tests/fixtures/yield_text_report/report*.json`` are synthetic reports (never
produced by the statistics engine) that take every conditional line of the
renderer on at least one measurement and skip it on another; the matching
``.txt`` is the exact text the renderer must print. Update a ``.txt`` only for
a deliberate, understood change to the report's layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import yield_campaign_cmd, yield_cmd

FIXTURES = Path(__file__).parent / "fixtures" / "yield_text_report"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "render"),
    [
        ("report", yield_cmd.print_yield_report),
        ("report_clean", yield_cmd.print_yield_report),
        ("report_campaign", yield_campaign_cmd._print_text),
    ],
)
def test_text_report_matches_golden(case, render, capsys):
    render(json.loads(_load(f"{case}.json")))
    assert capsys.readouterr().out == _load(f"{case}.txt")
