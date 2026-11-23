"""Guard for the live OpenROAD STA lane's marker registration (issue #7541).

`.github/workflows/post-route-sta-live.yml` provisions a real `openroad` +
the sky130A PDK and selects its tests with `-m live_openroad` instead of
the four hand-listed node IDs it used before, so a new live test joins the
lane **by construction** -- provided the `live_openroad` mark really is
applied wherever the live gate is. Nothing else enforces that link: an
inline `pytest.mark.skipif(_live_sky130_env() is None, ...)` written
without the mark compiles, collects, and passes in `ci.yml` while silently
never running in any lane -- the exact 17-of-21 drift this issue was filed
over.

These tests close that gap from the ordinary `test` job, without openroad:

1. **Registration** -- every `test*` function in `tests/` whose source
   references the live gate (`_live_sky130_env` / `_LIVE_ONLY`) carries the
   `live_openroad` mark on the collected function object.
2. **Non-vacuity** -- the shared gate names still exist and the decorator
   still applies both the marker and the skipif, so a rename that silently
   emptied the scan in (1) fails loudly here instead of passing vacuously.
3. **Lane wiring** -- the workflow still selects by `-m live_openroad` and
   does not narrow the selection back down to hand-listed node IDs, and the
   marker stays registered in `pyproject.toml` (without registration the
   mark is only an untyped warning).

The one in-body gate among the marked tests
(`test_pinned_engine_wire_capacitance_with_and_without_liberty_default`
skips when the live binary is not the pinned OpenROAD build) is satisfied
by the lane by construction: `scripts/install-openroad-docker.sh` pulls the
image digest whose bundled binary reports exactly that build.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent
LANE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "post-route-sta-live.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Names whose appearance in a test function's source means "live-gated".
# Both live in tests/test_post_route_sta.py; the shared `_LIVE_ONLY`
# decorator applies the skipif and the `live_openroad` mark together, so
# the normal way to write a live test satisfies the scan below for free.
GATE_NAMES = ("_live_sky130_env", "_LIVE_ONLY")


def _live_gated_tests() -> dict[str, tuple[str, int]]:
    """Map ``"<module>.<test>"`` -> file:line for every live-gated test.

    The scan is AST-based (no import needed to *find* candidates): a test
    function counts when its decorators or body reference either gate name.
    A module that merely imports `_live_sky130_env` for a helper does not
    count; only `test*` functions do.
    """
    gated: dict[str, tuple[str, int]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        # This guard's own tests reference the gate names to check them;
        # they are not live tests (they must run in the ordinary job).
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test")):
                continue
            source = ast.unparse(node)
            if any(name in source for name in GATE_NAMES):
                gated[f"{path.stem}.{node.name}"] = (str(path), node.lineno)
    return gated


def test_every_live_gated_test_carries_the_live_openroad_mark():
    gated = _live_gated_tests()
    assert gated, (
        "the scan found no live-gated tests; if the gate names changed, "
        "update GATE_NAMES and the lane's selection together"
    )
    files = {path for path, _ in gated.values()}
    assert len(files) >= 3, (
        f"expected the live gate to span several files, found {sorted(files)}"
    )
    unmarked = []
    for node_id in sorted(gated):
        module_name, func_name = node_id.split(".", 1)
        module = importlib.import_module(module_name)
        func = getattr(module, func_name, None)
        if func is None:
            unmarked.append(f"{node_id}: not found after import")
            continue
        marks = {mark.name for mark in getattr(func, "pytestmark", [])}
        if "live_openroad" not in marks:
            unmarked.append(f"{node_id}: gate present but live_openroad mark missing")
    assert not unmarked, (
        "live-gated tests without the live_openroad mark never run in any "
        "lane (issue #7541):\n  " + "\n  ".join(unmarked)
    )


def test_the_shared_live_gate_names_still_exist():
    """Anti-vacuity for the scan above.

    `_LIVE_ONLY` / `_live_sky130_env` are the only strings tying the scan
    to reality. If either is renamed, the scan would silently find nothing
    and pass; pin both names so the rename fails here instead.
    """
    module = importlib.import_module("test_post_route_sta")
    for name in GATE_NAMES:
        assert hasattr(module, name), (
            f"test_post_route_sta.{name} is gone; update GATE_NAMES in this "
            "guard and the lane's selection together"
        )


def test_LIVE_ONLY_applies_both_the_marker_and_the_skipif():
    """The shared decorator must keep bundling gate and registration.

    Decorating each test with a bare `pytest.mark.skipif(...)` again (the
    pre-#7541 style) would pass the workflow checks while dropping tests
    out of the lane; this pins that `_LIVE_ONLY` applies both marks.
    """
    module = importlib.import_module("test_post_route_sta")

    def _sample() -> None:
        pass

    decorated = module._LIVE_ONLY(_sample)
    marks = {mark.name for mark in getattr(decorated, "pytestmark", [])}
    assert "live_openroad" in marks
    assert "skipif" in marks


def test_the_live_lane_selects_by_marker_and_not_by_node_ids():
    workflow = LANE_WORKFLOW.read_text()
    match = re.search(
        r"- name: Run the live klt sta engine tests\n(.*?)(?=\n {6}- name: )",
        workflow,
        re.DOTALL,
    )
    assert match, "the 'Run the live klt sta engine tests' step is gone"
    run_step = match.group(1)
    assert re.search(r"pytest[^\n]*-m live_openroad", run_step), (
        "post-route-sta-live.yml no longer selects by -m live_openroad; "
        "hand-listed selections are exactly the drift issue #7541 fixed"
    )
    assert "::" not in run_step, (
        "the live lane narrows its selection to explicit node IDs; every "
        "unlisted live test runs in no lane again (issue #7541)"
    )


def _registered_marker_entries(pyproject_text: str) -> list[str]:
    """Flatten the `markers = [...]` list into its string entries.

    Continuation lines of a long entry (leading whitespace, not a new
    `"..."` item) fold into the previous entry so the first quoted token of
    each entry can be checked without a TOML parser.
    """
    markers_block = re.search(
        r"^markers\s*=\s*\[(.*?)^\]", pyproject_text, re.DOTALL | re.MULTILINE
    )
    assert markers_block, "pyproject.toml has no [tool.pytest.ini_options] markers list"
    entries: list[str] = []
    for line in markers_block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(('"', "'")) or not entries:
            entries.append(stripped)
        else:
            entries[-1] += " " + stripped
    return entries


def test_live_openroad_marker_is_registered_in_pyproject():
    entries = _registered_marker_entries(PYPROJECT.read_text())
    registered = any(
        line.startswith('"live_openroad:') or line.startswith("'live_openroad:")
        for line in entries
    )
    assert registered, (
        "the live_openroad marker must stay registered in pyproject.toml "
        "(an unregistered marker is only an untyped warning)"
    )


def test_the_scan_still_finds_the_tests_the_lane_was_filed_over():
    """Anchor the scan to concrete history, not just shape.

    `test_live_engine_spef_name_map_annotation` (the SPEF *NAME_MAP check
    the lane was created for in #3776) and
    `test_live_engine_report_instances` (#5754) must stay in the scan's
    reach: a future re-shaping of `_LIVE_ONLY` that loses them from the
    lane is precisely the regression this guard exists to catch.
    """
    gated = _live_gated_tests()
    for anchor in (
        "test_post_route_sta.test_live_engine_spef_name_map_annotation",
        "test_post_route_sta_instance_paths_run.test_live_engine_report_instances",
    ):
        assert anchor in gated
