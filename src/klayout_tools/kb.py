"""Query the circuit-design knowledge base (``kb/``) as plain Python data.

Pure library: every public function returns a plain ``dict`` of
JSON-serialisable primitives and never prints, mirroring ``layers.py`` /
``pdk.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/kb_cmd.py``).

``kb/`` is a flat-files data asset (see ``kb/README.md``) — one JSON file per
entry under ``kb/entries/``, validated against ``kb/schema/entry.schema.json``
— checked into this repo but not packaged into the wheel (it is a corpus for
an agent to read from a repo checkout, not runtime tool data). ``DEFAULT_KB_ROOT``
resolves to ``<repo root>/kb`` by walking up from this file's own location;
every function also accepts an explicit ``root`` for tests and any future
``--kb-root`` override.
"""

from __future__ import annotations

import json
import operator
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

#: <repo root>/kb — this file lives at <repo root>/src/klayout_tools/kb.py.
DEFAULT_KB_ROOT: Path = Path(__file__).resolve().parent.parent.parent / "kb"

#: Entry fields searched by :func:`search_entries`, per the issue's spec:
#: "case-insensitive keyword match over title, topology, spec_class,
#: layout_idioms, notes".
_SEARCH_FIELDS: tuple[str, ...] = (
    "title",
    "topology",
    "spec_class",
    "layout_idioms",
    "notes",
)

#: Fields returned by :func:`list_entries` / :func:`search_entries` summaries.
_SUMMARY_FIELDS: tuple[str, ...] = ("id", "title", "spec_class")


class KbError(Exception):
    """Raised when the KB cannot be read: missing directory/schema, an entry
    that isn't valid JSON, or a requested entry id that doesn't exist.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def _entries_dir(root: Path) -> Path:
    entries_dir = root / "entries"
    if not entries_dir.is_dir():
        raise KbError(f"kb entries directory not found: {entries_dir}")
    return entries_dir


def _schema_path(root: Path) -> Path:
    schema_path = root / "schema" / "entry.schema.json"
    if not schema_path.is_file():
        raise KbError(f"kb schema not found: {schema_path}")
    return schema_path


def _entry_paths(root: Path) -> list[Path]:
    return sorted(_entries_dir(root).glob("*.json"))


def _load_entry(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise KbError(f"{path.name}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise KbError(f"{path.name}: expected a JSON object at the top level")
    return data


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: entry.get(field) for field in _SUMMARY_FIELDS}


def list_entries(root: Path | None = None) -> dict[str, Any]:
    """Return ``id``/``title``/``spec_class`` for every entry, id-sorted.

    Raises :class:`KbError` if ``kb/entries/`` (or an entry file) can't be
    read.
    """
    root = root if root is not None else DEFAULT_KB_ROOT
    entries = [_summary(_load_entry(path)) for path in _entry_paths(root)]
    entries.sort(key=lambda entry: entry["id"] or "")
    return {"schema_version": 1, "count": len(entries), "entries": entries}


def show_entry(entry_id: str, root: Path | None = None) -> dict[str, Any]:
    """Return the full entry for ``entry_id``.

    Raises :class:`KbError` if no ``kb/entries/<entry_id>.json`` exists.
    """
    root = root if root is not None else DEFAULT_KB_ROOT
    path = _entries_dir(root) / f"{entry_id}.json"
    if not path.is_file():
        raise KbError(f"kb entry not found: {entry_id!r}")
    return {"schema_version": 1, "entry": _load_entry(path)}


def _matches(entry: dict[str, Any], needle: str) -> bool:
    for field in _SEARCH_FIELDS:
        value = entry.get(field)
        if value is None:
            continue
        haystack = " ".join(value) if isinstance(value, list) else str(value)
        if needle in haystack.lower():
            return True
    return False


#: Comparison operators accepted by a ``--where`` expression, longest-first
#: so ``>=``/``<=``/``==``/``!=`` are matched before the single-character
#: ``>``/``<`` they each contain as a substring.
_WHERE_OPERATORS: tuple[str, ...] = (">=", "<=", "==", "!=", ">", "<")

_WHERE_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
}


def _parse_where(expr: str) -> tuple[str, str, float]:
    """Parse one ``--where`` expression, e.g. ``"av_db>=40"``, into
    ``(figure_name, operator, value)``.

    Raises :class:`KbError` (not a bare ``ValueError``, so the CLI's
    existing ``KbError``-to-stderr-envelope handling covers this input error
    too) when the expression has no recognised operator, an empty figure
    name, or a non-numeric value.
    """
    for op in _WHERE_OPERATORS:
        index = expr.find(op)
        if index > 0:
            name = expr[:index].strip()
            raw_value = expr[index + len(op) :].strip()
            if not name:
                raise KbError(f"--where {expr!r}: missing figure name before {op!r}")
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise KbError(
                    f"--where {expr!r}: value is not a number: {raw_value!r}"
                ) from exc
            return name, op, value
    raise KbError(
        f"--where {expr!r}: expected '<figure_name><op><value>' with op one "
        f"of {', '.join(_WHERE_OPERATORS)}"
    )


def _entry_matches_where(
    entry: dict[str, Any], conditions: list[tuple[str, str, float]]
) -> bool:
    """An entry matches when, for *every* condition, at least one
    ``measured.figures[]`` entry of that name satisfies the comparison. An
    entry with no ``measured`` block (or missing the named figure) never
    matches a ``--where`` filter -- there is nothing numeric to compare.
    """
    measured = entry.get("measured")
    if not measured:
        return False
    values_by_name: dict[str, list[float]] = {}
    for figure in measured.get("figures") or []:
        values_by_name.setdefault(figure["name"], []).append(figure["value"])

    for name, op, threshold in conditions:
        values = values_by_name.get(name)
        if not values:
            return False
        comparator = _WHERE_COMPARATORS[op]
        if not any(comparator(value, threshold) for value in values):
            return False
    return True


def _entry_matches_pdk(entry: dict[str, Any], pdk: str) -> bool:
    """Case-insensitive exact match against ``measured.pdk``. An entry with
    no ``measured`` block never matches a ``--pdk`` filter."""
    measured = entry.get("measured")
    if not measured:
        return False
    return str(measured.get("pdk", "")).lower() == pdk.lower()


def search_entries(
    query: str = "",
    root: Path | None = None,
    where: list[str] | None = None,
    pdk: str | None = None,
) -> dict[str, Any]:
    """Case-insensitive keyword search over title/topology/spec_class/
    layout_idioms/notes, optionally narrowed by numeric ``measured.figures``
    filters (``where``, e.g. ``["av_db>=40"]``) and/or an exact
    ``measured.pdk`` match (``pdk``, e.g. ``"sky130"``). Stdlib-simple
    substring match for the keyword — no embeddings, no index
    (``kb/README.md``'s flat-files design).

    ``query`` may be empty (the default) to search on ``where``/``pdk``
    alone -- an empty keyword never excludes an entry, unlike a nonempty one
    against an entry whose searched fields are all absent.

    Raises :class:`KbError` if ``kb/entries/`` (or an entry file) can't be
    read, or if a ``where`` expression is malformed. An empty result is
    success, not an error.
    """
    root = root if root is not None else DEFAULT_KB_ROOT
    conditions = [_parse_where(expr) for expr in (where or [])]
    needle = query.lower()

    matches = []
    for entry in (_load_entry(path) for path in _entry_paths(root)):
        if needle and not _matches(entry, needle):
            continue
        if conditions and not _entry_matches_where(entry, conditions):
            continue
        if pdk and not _entry_matches_pdk(entry, pdk):
            continue
        matches.append(_summary(entry))

    matches.sort(key=lambda entry: entry["id"] or "")
    return {
        "schema_version": 1,
        "query": query,
        "where": list(where or []),
        "pdk": pdk,
        "count": len(matches),
        "entries": matches,
    }


#: `artifacts` keys checked for on-disk existence by :func:`_artifact_errors`.
#: Deliberately excludes ``artifacts.notes``, which is prose, not a path.
_ARTIFACT_PATH_FIELDS: tuple[str, ...] = ("netlist", "layout")


def _repo_relative_path_error(rel_path: str, label: str, repo_root: Path) -> str | None:
    """Shared path-existence/containment check used by both
    :func:`_artifact_errors` and :func:`_measured_errors`.

    A path that is absolute, or that escapes the repository root via ``..``,
    is reported as an error rather than resolved: ``repo_root / "/etc/passwd"``
    is ``/etc/passwd``, so an unguarded join would let an entry "verify"
    against a file outside the repo that no other checkout can see.
    """
    candidate = Path(rel_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return (
            f"{label}: must be a repository-relative path without '..' "
            f"segments: {rel_path}"
        )
    if not (repo_root / candidate).is_file():
        return f"{label}: referenced path does not exist: {rel_path}"
    return None


def _artifact_errors(entry: dict[str, Any], repo_root: Path) -> list[str]:
    """Check that any ``artifacts.{netlist,layout}`` path referenced by
    ``entry`` exists on disk, relative to the repository root (``kb/``'s
    parent directory) -- e.g. ``artifacts.netlist: "examples/..."``.

    Absent/null ``artifacts`` (or an absent individual key) is not an error;
    the schema already enforces that at least one key is present when the
    object itself is given.
    """
    artifacts = entry.get("artifacts")
    if not artifacts:
        return []

    errors = []
    for field in _ARTIFACT_PATH_FIELDS:
        rel_path = artifacts.get(field)
        if not rel_path:
            continue
        error = _repo_relative_path_error(rel_path, f"artifacts/{field}", repo_root)
        if error:
            errors.append(error)
    return errors


def _measured_errors(entry: dict[str, Any], repo_root: Path) -> list[str]:
    """Check that every ``measured.figures[].testbench`` path referenced by
    ``entry`` exists on disk, relative to the repository root -- the same
    check :func:`_artifact_errors` runs for ``artifacts.{netlist,layout}``.

    Absent/null ``measured`` is not an error; the schema already requires
    ``figures`` (and each figure's ``testbench``) when ``measured`` itself
    is given.
    """
    measured = entry.get("measured")
    if not measured:
        return []

    errors = []
    for index, figure in enumerate(measured.get("figures") or []):
        rel_path = figure.get("testbench")
        if not rel_path:
            continue
        label = f"measured/figures/{index}/testbench"
        error = _repo_relative_path_error(rel_path, label, repo_root)
        if error:
            errors.append(error)
    return errors


def _entry_errors(
    path: Path, validator: jsonschema.protocols.Validator, repo_root: Path
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        entry = _load_entry(path)
    except KbError as exc:
        return None, [str(exc)]

    errors = []
    for error in sorted(validator.iter_errors(entry), key=str):
        location = "/".join(str(part) for part in error.path)
        errors.append(f"{location}: {error.message}" if location else error.message)

    entry_id = entry.get("id")
    if entry_id != path.stem:
        errors.append(f"id {entry_id!r} does not match filename stem {path.stem!r}")

    errors.extend(_artifact_errors(entry, repo_root))
    errors.extend(_measured_errors(entry, repo_root))

    return entry, errors


def validate_entries(
    root: Path | None = None, repo_root: Path | None = None
) -> dict[str, Any]:
    """Validate every entry: well-formed JSON, conforms to
    ``kb/schema/entry.schema.json``, ``id`` matches its filename stem, and
    any ``artifacts.{netlist,layout}`` or ``measured.figures[].testbench``
    path it references exists on disk (resolved relative to the repository
    root).

    ``repo_root`` is the base those paths resolve against; it defaults to
    ``root``'s parent, which is the real layout of this repo (``kb/`` sits
    at the top level). Pass it explicitly when the KB root is not a direct
    child of the tree the artifacts live in — e.g. a test that builds a
    throwaway ``kb/`` beside a throwaway ``examples/``.

    Never raises for entry-level problems (they show up as structured
    per-entry errors in the returned payload, per the issue's spec); still
    raises :class:`KbError` if ``kb/entries/`` or the schema file itself is
    missing/unreadable, which is an environment problem, not a validation
    finding.
    """
    root = root if root is not None else DEFAULT_KB_ROOT
    schema = json.loads(_schema_path(root).read_text())
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)

    repo_root = repo_root if repo_root is not None else root.parent
    results = []
    for path in _entry_paths(root):
        _entry, errors = _entry_errors(path, validator, repo_root)
        results.append({"id": path.stem, "valid": not errors, "errors": errors})

    return {
        "schema_version": 1,
        "valid": all(result["valid"] for result in results),
        "entry_count": len(results),
        "entries": results,
    }
