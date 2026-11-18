"""Item 9 ``depends_on`` declarations (issue #5850).

Split out of :mod:`klayout_tools.signoff_attest_item9` (file-size ratchet). A
testbench entry may declare ``depends_on`` -- repository-relative paths the
bench depends on (a helper it imports, a model card it includes). Each
declared dependency is hashed into the envelope as its own ``present`` check,
so editing it is caught by ``--check`` instead of passing green; the
per-member ``dependency_binding`` disclosure records how many edges were
declared, because binding is otherwise shallow.

Like its parent, the validator takes the path helper as an argument and the
check builder takes the file-check helper, so this module need not import
:mod:`signoff_attest` (which imports it) at load time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def validated_depends_on(
    raw: Any,
    field: str,
    root: str,
    normalize_path: Callable[[Any, str, str], str],
) -> list[str]:
    """One entry's ``depends_on`` (issue #5850): a list of repo-relative
    paths under the ordinary path rules (non-empty, relative, no ``..``, no
    symlink traversal). An empty list is accepted -- the bench declares it
    has no first-party dependencies. Sorted, and deduplicated per entry.

    The paths are validated per entry and deliberately do not join the
    inventory's shared path-uniqueness dict: a helper shared by two benches
    is the normal case (the same exemption item 1's ``recorded.sources``
    enjoys), so a dependency may repeat across entries and may itself be an
    inventoried member -- only a repeat *within* one entry is refused."""
    if not isinstance(raw, list):
        raise _error(
            f"'{field}.depends_on' must be a list of repository-relative paths"
        )
    seen: set[str] = set()
    deps: list[str] = []
    for dep in raw:
        normalized = normalize_path(dep, f"{field}.depends_on", root)
        if normalized in seen:
            raise _error(f"'{field}.depends_on' lists '{normalized}' more than once")
        seen.add(normalized)
        deps.append(normalized)
    return sorted(deps)


def dependency_present_checks(
    root: str,
    normalized: Mapping[str, Any],
    file_check: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """One ``present`` check per declared dependency edge (issue #5850),
    benches sorted by path (each entry's dependencies were sorted at
    validation), so a missing or edited dependency names both itself and the
    bench that declared it. ``file_check`` is the caller-injected
    :func:`signoff_attest._file_check` reused unchanged (injected, per this
    module's import boundary, rather than imported); the owning ``testbench``
    is recorded after ``role``, where the per-bench cold-start checks carry
    theirs."""
    checks = []
    for bench in sorted(normalized["bench_depends_on"]):
        for dep in normalized["bench_depends_on"][bench]:
            base = file_check(root, dep, "testbench_dependency")
            checks.append(
                {
                    "check": base["check"],
                    "role": "testbench_dependency",
                    "testbench": bench,
                    "path": base["path"],
                    "passed": base["passed"],
                    "content_hash": base["content_hash"],
                }
            )
    return checks


def dependency_binding_checks(normalized: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One visible, **passing** check per member recording how many
    dependency edges it declared (issue #5850). Member binding is shallow: a
    bench's own dependencies are resolved and hashed only when ``depends_on``
    declares them, and the envelope must say so plainly -- a member list and
    a green ``--check`` must not read as complete coverage. Whether an
    undeclared dependency should fail T1 item 9 is a grading-policy decision
    this producer does not make, so the check passes at ``declared: 0``."""
    depends_on = normalized["bench_depends_on"]
    return [
        {
            "check": "dependency_binding",
            "role": "testbench",
            "testbench": path,
            "declared": len(depends_on.get(path, ())),
            "passed": True,
        }
        for path in sorted(normalized["testbenches"])
    ]
