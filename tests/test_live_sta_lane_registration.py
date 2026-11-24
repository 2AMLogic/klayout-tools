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

1. **Registration** -- live-gated test functions and Test-class methods
   carry the `live_openroad` mark on the function or its collecting class.
2. **Non-vacuity** -- the shared gate names still exist and the decorator
   still applies both the marker and the skipif, so a rename that silently
   emptied the scan in (1) fails loudly here instead of passing vacuously.
3. **Lane wiring** -- the workflow derives its node IDs from a
   `--collect-only -m live_openroad` pass and runs exactly those (never a
   bare `-m` run over the whole `tests/` tree, whose collection-time skips
   in unrelated modules would trip the lane's own SKIPPED gate; never
   hand-listed node IDs), and the marker stays registered in
   `pyproject.toml` (without registration the mark is only an untyped
   warning).

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


def _references_gate(nodes: list[ast.AST], *, skip_nested_defs: bool) -> bool:
    """Walk one scope for a gate reference.

    ``skip_nested_defs=True`` is for a *class* scope: a method's own gate
    must not gate its un-gated siblings, so nested definitions are not
    entered. ``False`` is for a *function* scope: a gate reached only
    through a nested ``def`` (``_setup()`` returning ``_live_sky130_env()``)
    still gates the enclosing test, so the body is walked recursively
    (issue #7706). Nested ``test*`` helpers are still never yielded as their
    own entries -- :func:`_gated_functions` only yields direct children.
    """
    pending = list(nodes)
    while pending:
        node = pending.pop()
        if skip_nested_defs and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if isinstance(node, ast.Name) and node.id in GATE_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in GATE_NAMES:
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _gated_functions(nodes, *, prefix="", class_gated=False):
    for node in nodes:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test"):
            kw_defaults = [
                value for value in node.args.kw_defaults if value is not None
            ]
            if class_gated or _references_gate(
                [*node.decorator_list, *node.args.defaults, *kw_defaults, *node.body],
                skip_nested_defs=False,
            ):
                yield prefix + node.name, node.lineno


def _live_gated_tests(directory: Path = TESTS_DIR) -> dict[str, tuple[str, int]]:
    """Map collected function and method names to the locations using a live gate."""
    gated = {}
    for path in sorted(directory.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text())
        functions = list(_gated_functions(tree.body))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                functions.extend(
                    _gated_functions(
                        node.body,
                        prefix=node.name + ".",
                        class_gated=_references_gate(
                            [*node.decorator_list, *node.body],
                            skip_nested_defs=True,
                        ),
                    )
                )
        for name, line in functions:
            gated[f"{path.stem}.{name}"] = (str(path), line)
    return gated


def _unmarked_gated_tests(gated):
    unmarked = []
    for node_id in sorted(gated):
        module, *qualname = node_id.split(".")
        owner = importlib.import_module(module)
        marks = set()
        for name in qualname:
            owner = getattr(owner, name, None)
            if owner is None:
                break
            declared = getattr(owner, "pytestmark", [])
            if not isinstance(declared, (list, tuple)):
                declared = [declared]
            marks.update(mark.name for mark in declared)
        if owner is None:
            unmarked.append(f"{node_id}: not found after import")
        elif "live_openroad" not in marks:
            unmarked.append(f"{node_id}: gate present but live_openroad mark missing")
    return unmarked


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
    unmarked = _unmarked_gated_tests(gated)
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


def _live_lane_run_commands() -> list[str]:
    """Logical shell command lines of the lane's "Run the live ..." step.

    Comment lines are dropped (a comment must never satisfy a wiring
    check) and ``\\``-continued lines are joined, so each pipeline is one
    string regardless of how the workflow wraps it.
    """
    match = re.search(
        r"- name: Run the live klt sta engine tests\n(.*?)(?=\n {6}- name: )",
        LANE_WORKFLOW.read_text(),
        re.DOTALL,
    )
    assert match, "the 'Run the live klt sta engine tests' step is gone"
    code = "\n".join(
        line
        for line in match.group(1).splitlines()
        if not line.lstrip().startswith("#")
    )
    return [line.strip() for line in code.replace("\\\n", " ").splitlines()]


_EXPLICIT_TEST_NODE = re.compile(r"tests/\S+\.py::\w")


def test_node_id_check_allows_workflow_annotations():
    assert not _EXPLICIT_TEST_NODE.search("echo ::group::Tests")
    assert not _EXPLICIT_TEST_NODE.search("echo ::notice::collected")
    assert _EXPLICIT_TEST_NODE.search("pytest tests/test_sta.py::TestSTA::test_live")


def test_the_live_lane_selects_by_marker_and_not_by_node_ids():
    commands = _live_lane_run_commands()
    pytest_cmds = [c for c in commands if re.search(r"\bpytest\b", c)]
    collect = [c for c in pytest_cmds if "--collect-only" in c]
    run = [c for c in pytest_cmds if "--collect-only" not in c]

    # The selection is *derived from the marker*: a collect-only pass with
    # `-m live_openroad` yields the node IDs the lane then runs.
    assert any("-m live_openroad" in c for c in collect), (
        "post-route-sta-live.yml no longer derives its selection from "
        "`pytest --collect-only -m live_openroad`; hand-listed selections "
        "are exactly the drift issue #7541 fixed"
    )
    # An empty selection must fail the step, not run nothing / run all.
    assert any(re.match(r"test -s \S+", c) for c in commands), (
        "the lane no longer fails loudly when the marker selects nothing "
        "(`test -s <node-id file>`)"
    )
    # The real run executes exactly the derived IDs. A bare `-m` run over
    # `testpaths` collects the whole tree, so unrelated modules'
    # module-level skips land in the log and trip the lane's own SKIPPED
    # gate on a fully successful run (PR #7550 review, run 45197663830);
    # a run with no selection argument would run all 30k tests.
    assert run, "the lane no longer runs pytest after the collect-only pass"
    for cmd in run:
        assert "-m live_openroad" not in cmd, (
            "the live run selects with a bare `-m live_openroad` over the "
            f"whole tests/ tree; collection-time skips of unrelated modules "
            f"then fail the SKIPPED gate: {cmd!r}"
        )
        assert '"${nodes[@]}"' in cmd, (
            f"the live run does not consume the derived node IDs: {cmd!r}"
        )
    # GitHub workflow annotations may also contain double colons.
    for cmd in commands:
        assert not _EXPLICIT_TEST_NODE.search(cmd), (
            "the live lane lists explicit node IDs; every unlisted live "
            f"test runs in no lane again (issue #7541): {cmd!r}"
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


def test_class_and_method_gates_match_their_runtime_marks(tmp_path, monkeypatch):
    module = "test_live_registration_fixture"
    (tmp_path / f"{module}.py").write_text("""
import pytest
_live_sky130_env = lambda: False
_LIVE_ONLY = pytest.mark.skipif(not _live_sky130_env(), reason="fixture")

def test_positional_default(pdk=_live_sky130_env()):
    if not pdk:
        pytest.skip("live PDK unavailable")

class TestMethods:
    def test_keyword_default(self, *, pdk=_live_sky130_env()):
        if not pdk:
            pytest.skip("live PDK unavailable")

    def test_unmarked(self):
        _live_sky130_env()

    @pytest.mark.live_openroad
    def test_marked(self):
        _live_sky130_env()

    def test_gated_via_nested_helper(self):
        def test_helper(pdk=_live_sky130_env()):
            _live_sky130_env()

@_LIVE_ONLY
@pytest.mark.live_openroad
class TestClassGate:
    def test_inherits(self):
        pass

class TestBodyGate:
    pytestmark = pytest.mark.skipif(not _live_sky130_env(), reason="fixture")
    def test_unmarked(self):
        pass

class Uncollected:
    @_LIVE_ONLY
    def test_helper(self):
        pass
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = _live_gated_tests(tmp_path)
    expected = (
        "test_positional_default",
        "TestMethods.test_keyword_default",
        "TestMethods.test_unmarked",
        "TestMethods.test_marked",
        "TestMethods.test_gated_via_nested_helper",
        "TestClassGate.test_inherits",
        "TestBodyGate.test_unmarked",
    )
    assert set(found) == {f"{module}.{name}" for name in expected}
    # The enclosing method is gated through its nested helper, but the nested
    # `test_helper` is not collected by pytest and is never its own entry.
    assert not any(node_id.endswith("test_helper") for node_id in found)
    assert _unmarked_gated_tests(found) == [
        f"{module}.{name}: gate present but live_openroad mark missing"
        for name in (
            "TestBodyGate.test_unmarked",
            "TestMethods.test_gated_via_nested_helper",
            "TestMethods.test_keyword_default",
            "TestMethods.test_unmarked",
            "test_positional_default",
        )
    ]


def test_gate_reached_only_through_a_nested_def_is_detected(tmp_path):
    """A closure-held gate still gates the enclosing collected test (#7706).

    The nested-scope skip in `_references_gate` is only right for a *class*
    body (else one gated method would gate every sibling); a function's own
    body must be walked through its nested defs, or a live test that reaches
    `_live_sky130_env()` via a `_setup()` helper falls out of the lane.
    """
    (tmp_path / "test_live_nested_fixture.py").write_text("""
import pytest
_live_sky130_env = lambda: False
_LIVE_ONLY = pytest.mark.skipif(not _live_sky130_env(), reason="fixture")

def test_module_nested_setup():
    def _setup():
        return _live_sky130_env()
    if not _setup():
        pytest.skip("live PDK unavailable")

async def test_async_nested_setup():
    def _setup():
        return _live_sky130_env()
    assert _setup()

def test_outer_with_nested_test_helper():
    def test_inner_helper(pdk=_live_sky130_env()):
        return pdk
    return test_inner_helper

def test_plain_with_nested_plain_helper():
    def _setup():
        return 1
    assert _setup()

class TestNested:
    def test_live_method_uses_gate_via_nested_helper(self):
        def _setup():
            return _live_sky130_env()
        assert _setup()

    def test_plain_sibling(self):
        def _setup():
            return 1
        assert _setup()

class TestSiblings:
    @_LIVE_ONLY
    def test_decorated(self):
        pass

    def test_plain_sibling(self):
        pass
""")
    found = _live_gated_tests(tmp_path)
    expected = (
        "test_module_nested_setup",
        "test_async_nested_setup",
        "test_outer_with_nested_test_helper",
        "TestNested.test_live_method_uses_gate_via_nested_helper",
        "TestSiblings.test_decorated",
    )
    assert set(found) == {f"test_live_nested_fixture.{name}" for name in expected}
    # A nested `test*` helper is never reported as its own entry.
    assert not any("test_inner_helper" in node_id for node_id in found)
    # One gated method must not gate its un-gated siblings, including when
    # its own gate is held by a nested def.
    assert "test_live_nested_fixture.TestSiblings.test_plain_sibling" not in found
    assert "test_live_nested_fixture.TestNested.test_plain_sibling" not in found
