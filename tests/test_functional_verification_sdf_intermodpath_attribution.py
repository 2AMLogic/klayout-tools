"""Issue #5769: resolving an intermodpath refusal's ``<file>:<line>``
locators onto the SDF entries Icarus actually rejected.

The shared refusal names a confirmed-rejected arc only when its
diagnostic resolves onto a one-line, non-zero ``INTERCONNECT`` entry from
a bit-selected top-level input port in one of the run's annotation files
-- including the #1619 split's deferred copy, whose locators count lines
cumulatively across ``$sdf_annotate`` calls (a 16-line safe file plus
line 10 of the deferred file is reported as line 26). These tests drive
the resolver and the hint directly against hand-staged files; the
refusal-path end-to-ends (cocotb stub runner, verilog-top gate, real
Icarus) live in ``test_functional_verification_sdf_refusal_details.py``
and ``test_functional_verification.py``.
"""

from __future__ import annotations

from pathlib import Path

from klayout_tools.functional_verification_sdf_steps import (
    _sdf_intermodpath_rejected_arcs,
    _sdf_refusal_error,
)

_UNRELATED_ERROR_LINE = (
    "SDF ERROR: route.sdf:2976: Unable to match ModPath D -> Q in gcd._647_"
)


def _intermodpath_line(path: Path, line: int) -> str:
    return f"SDF ERROR: {path}:{line}: Could not find intermodpath!"


def _stage_resolver_files(tmp_path: Path) -> tuple[Path, Path]:
    """A 16-line safe file and a deferred file (in a path with spaces)
    whose lines 10-11 carry the two bus-port entries -- the cumulative
    offsets below are stated against these counts."""
    safe = tmp_path / "klt_sdf_safe.sdf"
    safe.write_text("safe\n" * 16, encoding="utf-8")
    spacey = tmp_path / "sp ace" / "klt sdf deferred.sdf"
    spacey.parent.mkdir()
    lines = ["x"] * 9 + [
        "(INTERCONNECT a[0] u1.in (1.000:5.000:9.000))",
        "(INTERCONNECT a[1] u2.in (0.000:0.000:0.000))",
    ]
    spacey.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return safe, spacey


def test_rejected_arcs_resolve_cumulative_locators(tmp_path):
    """Line 26 of the deferred file = 16 safe lines + its line 10: the
    non-zero entry is named. Its neighbour (line 27) is zero-delay, so it
    is never named -- #2984's class owns it. A repeated locator names the
    arc once."""
    _, spacey = _stage_resolver_files(tmp_path)
    diagnostics = [
        _intermodpath_line(spacey, 26),
        _intermodpath_line(spacey, 27),
        _intermodpath_line(spacey, 26),
    ]

    arcs = _sdf_intermodpath_rejected_arcs(
        diagnostics, [str(tmp_path / "klt_sdf_safe.sdf"), str(spacey)], {"a"}
    )

    assert arcs == ["a[0] → u1.in"]


def test_rejected_arcs_skip_unresolvable_or_unrelated(tmp_path):
    """Nothing is named for: an out-of-range line, a file the run never
    annotated, a non-intermodpath diagnostic, and an entry whose source
    port is not among the run's bus input ports."""
    _, spacey = _stage_resolver_files(tmp_path)
    annotate_paths = [str(tmp_path / "klt_sdf_safe.sdf"), str(spacey)]
    cases = [
        ([_intermodpath_line(spacey, 99)], {"a"}),  # past the file's end
        ([_intermodpath_line(tmp_path / "elsewhere.sdf", 10)], {"a"}),
        ([_UNRELATED_ERROR_LINE], {"a"}),  # Unable to match ModPath
        ([_intermodpath_line(spacey, 26)], set()),  # a is not an input
    ]

    for diagnostics, ports in cases:
        assert _sdf_intermodpath_rejected_arcs(diagnostics, annotate_paths, ports) == []


def test_refusal_lists_five_arcs_and_counts_the_rest(tmp_path):
    """Seven confirmed rejections name five distinct arcs and count the
    remaining two -- the same cap the diagnostic summary itself uses."""
    annotate_path = tmp_path / "deferred.sdf"
    lines = ["x"] * 9 + [
        f"(INTERCONNECT a[{i}] u{i}.in (1.000:5.000:9.000))" for i in range(7)
    ]
    annotate_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    diagnostics = [_intermodpath_line(annotate_path, 10 + i) for i in range(7)]
    sdf = {
        "file": "route.sdf",
        "annotate_paths": [str(annotate_path)],
        "bus_input_ports": {"a"},
    }

    message = str(_sdf_refusal_error(sdf, "icarus", diagnostics, {}))

    for i in range(5):
        assert f"a[{i}] → u{i}.in" in message
    assert "a[5] → u5.in" not in message
    assert "(+2 more)" in message
