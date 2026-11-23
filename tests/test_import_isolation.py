"""Runtime guard: ``*_errors`` leaves keep their orchestrator unloaded (#7299).

The ``*_errors`` leaf modules let sibling helpers raise a verb's exception
at module scope without loading its orchestrator (#7181, #7185, #7210,
#7217, #7220, #7224, #7247). ``scripts/check_import_boundaries.py`` is a
static group-edge lint: every module here may already import the
orchestrator, so re-adding a module-scope ``from .sim import SimError``
to a helper would pass that gate and every test while silently undoing
the measured win. Each case here imports modules in fresh ``python -I``
children and asserts the orchestrator stays out of ``sys.modules``.

Leaves are auto-discovered via ``src/klayout_tools/*_errors.py``
(orchestrator = stem minus ``_errors``, skipped when no such module), so
new leaves are covered with no pair list to rot. Consumers are
auto-discovered too, as the union of:

* rule (a): any module importing ``from .<leaf> import ...`` at any
  scope; and
* rule (b): any module whose *module-scope* imports from
  ``.<orchestrator>`` name only classes the leaf defines -- the exact
  regression shape of a helper swapping its leaf import for the
  orchestrator's.

Cases stay parametrized per consumer for attribution, but process spawns
are batched per leaf to hold the added suite time under the ~10 s budget
measured in #7299 (~65 consumer spawns cost ~4-11 s depending on host
load; one spawn per leaf costs a fraction of that). A batch imports the
leaf's consumers cumulatively in sorted order and stops at the first one
that loads the orchestrator: a clean cumulative run proves every member
clean, because an isolated import can only load a subset. A batch that
trips, or crashes anywhere in the cumulative chain (a swapped leaf
import can build a real import cycle), re-probes consumers one by one in
fresh children so the failing run names exactly the offenders. Consumers
listed in ``NAMED_EXCEPTIONS`` (deliberate module-scope orchestrator
loads) are always probed individually, in fresh children, so a stale
entry fails.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "klayout_tools"

# Deliberate module-scope orchestrator loads, keyed by consumer module,
# each value the reason the load is by design. The map applies to
# consumers from either discovery rule and rots in either direction: a
# consumer that stops loading its orchestrator, or stops being a
# discovered consumer at all, fails below and must be removed from here.
NAMED_EXCEPTIONS = {
    "equiv_sequential": "imports equiv at module scope by design; "
    "equiv loads it lazily",
}

# Child probes. The LOADED/ABSENT/CLEAN/TRIGGERED sentinel on stdout
# makes an ImportError in the child fail loudly instead of reading as
# "orchestrator absent". ``-I`` ignores PYTHONPATH and user site; the
# package resolves through the (editable) install of the venv this suite
# runs under, so CI and worktrees behave the same.
_PROBE = (
    "import sys\n"
    "__import__(sys.argv[1])\n"
    "print('LOADED' if sys.argv[2] in sys.modules else 'ABSENT')\n"
)
_BATCH_PROBE = (
    "import sys\n"
    "target = sys.argv[1]\n"
    "for consumer in sys.argv[2:]:\n"
    "    __import__('klayout_tools.' + consumer)\n"
    "    if target in sys.modules:\n"
    "        print('TRIGGERED ' + consumer)\n"
    "        break\n"
    "else:\n"
    "    print('CLEAN')\n"
)
_PROBE_TIMEOUT_S = 120


def _leaf_pairs() -> list[tuple[str, str]]:
    """(leaf, orchestrator) per ``*_errors`` module with an orchestrator."""
    pairs: list[tuple[str, str]] = []
    for path in sorted(SRC_DIR.glob("*_errors.py")):
        leaf = path.stem
        orchestrator = leaf.removesuffix("_errors")
        has_orchestrator = (SRC_DIR / f"{orchestrator}.py").exists() or (
            SRC_DIR / orchestrator
        ).is_dir()
        if has_orchestrator:
            pairs.append((leaf, orchestrator))
    return pairs


def _leaf_class_names(path: Path) -> set[str]:
    """Top-level class names a leaf defines (rule (b)'s name pool)."""
    tree = ast.parse(path.read_text())
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _runtime_imports(node: ast.AST) -> Iterator[ast.ImportFrom]:
    """Yield imports in executable module statements, including try/if suites."""
    if isinstance(node, ast.ImportFrom):
        yield node
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    if isinstance(node, ast.If):
        test = node.test
        runtime_branch = node.orelse
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            test = test.operand
            runtime_branch = node.body
        if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        ):
            for statement in runtime_branch:
                yield from _runtime_imports(statement)
            return
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
            yield from _runtime_imports(child)


def _discover_consumers(
    directory: Path, pairs: list[tuple[str, str]]
) -> dict[str, set[str]]:
    """Discover all pairs in one pass over the package's source files."""
    classes = {leaf: _leaf_class_names(directory / f"{leaf}.py") for leaf, _ in pairs}
    consumers: dict[str, set[str]] = {leaf: set() for leaf in classes}
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text())
        leaf_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1
        }
        runtime_names: dict[str, set[str]] = {}
        for node in _runtime_imports(tree):
            if node.level == 1 and node.module:
                runtime_names.setdefault(node.module, set()).update(
                    alias.name for alias in node.names
                )
        for leaf, orchestrator in pairs:
            if path.stem in (leaf, orchestrator):
                continue
            names = runtime_names.get(orchestrator, set())
            if leaf in leaf_imports or (names and names <= classes[leaf]):
                consumers[leaf].add(path.stem)
    return consumers


def test_consumer_discovery_respects_runtime_scope_and_all_imported_names(
    tmp_path: Path,
) -> None:
    sources = {
        "sim_errors": "class SimError(Exception): pass",
        "sim": "from .sim_errors import SimError",
        "single": "from .sim import SimError as Error",
        "separate": "from .sim import SimError\nfrom .sim import run_sim",
        "combined": "from .sim import SimError, run_sim",
        "optional": (
            "try:\n    from .sim import SimError\nexcept ImportError:\n    pass"
        ),
        "handler": "try:\n    pass\nexcept ImportError:\n    from .sim import SimError",
        "nested": (
            "if enabled:\n    if TYPE_CHECKING:\n        from .sim import SimError"
        ),
        "conditional": (
            "if typing.TYPE_CHECKING:\n    from .sim import run_sim\n"
            "else:\n    from .sim import SimError"
        ),
        "runtime": "if not TYPE_CHECKING:\n    from .sim import SimError",
        "function": "def run():\n    from .sim import SimError",
        "leaf_in_function": "def run():\n    from .sim_errors import SimError",
    }
    for module, source in sources.items():
        (tmp_path / f"{module}.py").write_text(source)
    assert _discover_consumers(tmp_path, [("sim_errors", "sim")]) == {
        "sim_errors": {
            "single",
            "optional",
            "handler",
            "conditional",
            "runtime",
            "leaf_in_function",
        }
    }


_LEAF_PAIRS = _leaf_pairs()
_LEAF_CONSUMERS = _discover_consumers(SRC_DIR, _LEAF_PAIRS)
_CONSUMER_CASES = [
    pytest.param(leaf, orchestrator, consumer, id=f"{consumer}-{leaf}")
    for leaf, orchestrator in _LEAF_PAIRS
    for consumer in sorted(_LEAF_CONSUMERS[leaf])
]


class _ProbeFailure(Exception):
    """A probe child crashed or printed no sentinel; args carry the detail."""


def _run_child(code: str, args: list[str], context: str) -> str:
    """Run one probe child and return its sentinel line, failing loudly."""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
    )
    state = proc.stdout.strip()
    if proc.returncode != 0 or not state:
        raise _ProbeFailure(
            f"import-isolation probe for {context} crashed instead of "
            f"reporting: rc={proc.returncode}, stdout={state!r}, stderr "
            f"tail={proc.stderr.strip()[-400:]!r}"
        )
    return state


def _probe(module: str, target: str) -> str:
    """Import ``klayout_tools.<module>`` fresh; report ``<target>``'s state."""
    return _run_child(
        _PROBE,
        [f"klayout_tools.{module}", f"klayout_tools.{target}"],
        f"{module} (target {target})",
    )


_VERDICTS: dict[tuple[str, str], dict[str, str]] = {}


def _consumer_verdicts(leaf: str, orchestrator: str) -> dict[str, str]:
    """Per-consumer verdicts for one leaf, batched on the clean path.

    Clean path: one child imports every non-exception consumer
    cumulatively; CLEAN means each is ABSENT in isolation too. Any other
    outcome -- a consumer tripping the orchestrator, a crash inside the
    cumulative chain (a swapped leaf import can build a real import
    cycle) -- falls back to one child per consumer so exactly the
    offenders, and only they, are named by their own cases.
    """
    key = (leaf, orchestrator)
    if key not in _VERDICTS:
        consumers = sorted(
            consumer
            for consumer in _LEAF_CONSUMERS[leaf]
            if consumer not in NAMED_EXCEPTIONS
        )
        try:
            state = _run_child(
                _BATCH_PROBE,
                [f"klayout_tools.{orchestrator}", *consumers],
                f"{leaf} consumer batch ({len(consumers)} modules, target "
                f"{orchestrator})",
            )
        except _ProbeFailure:
            state = "ATTRIBUTE"
        if state == "CLEAN":
            verdicts = {consumer: "ABSENT" for consumer in consumers}
        else:
            verdicts = {}
            for consumer in consumers:
                try:
                    verdicts[consumer] = _probe(consumer, orchestrator)
                except _ProbeFailure as exc:
                    verdicts[consumer] = f"CRASHED: {exc}"
        _VERDICTS[key] = verdicts
    return _VERDICTS[key]


@pytest.mark.parametrize(
    ("leaf", "orchestrator"), _LEAF_PAIRS, ids=[leaf for leaf, _ in _LEAF_PAIRS]
)
def test_leaf_import_keeps_orchestrator_unloaded(leaf: str, orchestrator: str) -> None:
    try:
        state = _probe(leaf, orchestrator)
    except _ProbeFailure as exc:
        pytest.fail(str(exc))
    assert state == "ABSENT", (
        f"importing klayout_tools.{leaf} loads "
        f"klayout_tools.{orchestrator}: the leaf must stay importable "
        f"without its orchestrator or every measured module-count win of "
        f"the split is gone"
    )


@pytest.mark.parametrize(("leaf", "orchestrator", "consumer"), _CONSUMER_CASES)
def test_consumer_import_keeps_orchestrator_unloaded(
    leaf: str, orchestrator: str, consumer: str
) -> None:
    if consumer in NAMED_EXCEPTIONS:
        try:
            state = _probe(consumer, orchestrator)
        except _ProbeFailure as exc:
            pytest.fail(str(exc))
        assert state == "LOADED", (
            f"NAMED_EXCEPTIONS['{consumer}'] is stale: importing "
            f"klayout_tools.{consumer} no longer loads "
            f"klayout_tools.{orchestrator} -- remove the entry "
            f"(reason was: {NAMED_EXCEPTIONS[consumer]!r})"
        )
    else:
        state = _consumer_verdicts(leaf, orchestrator)[consumer]
        if state == "LOADED":
            pytest.fail(
                f"importing klayout_tools.{consumer} loads "
                f"klayout_tools.{orchestrator}, undoing the {leaf} split: "
                f"move the orchestrator import inside a function, or "
                f"import the exception class from klayout_tools.{leaf} "
                f"instead"
            )
        if state.startswith("CRASHED: "):
            pytest.fail(
                f"importing klayout_tools.{consumer} fails on its own "
                f"({state.removeprefix('CRASHED: ')}); if a leaf import "
                f"was swapped for the orchestrator's, restore it, move it "
                f"inside a function, or import the exception class from "
                f"klayout_tools.{leaf} instead"
            )
        assert state == "ABSENT"


def test_named_exceptions_still_name_discovered_consumers() -> None:
    discovered = {
        consumer for consumers in _LEAF_CONSUMERS.values() for consumer in consumers
    }
    unknown = sorted(set(NAMED_EXCEPTIONS) - discovered)
    assert not unknown, (
        f"NAMED_EXCEPTIONS names modules that are not discovered "
        f"consumers of any *_errors leaf (remove or re-check): {unknown}"
    )
