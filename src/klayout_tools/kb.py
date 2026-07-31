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


def search_entries(query: str, root: Path | None = None) -> dict[str, Any]:
    """Case-insensitive keyword search over title/topology/spec_class/
    layout_idioms/notes. Stdlib-simple substring match — no embeddings, no
    index (``kb/README.md``'s flat-files design).

    Raises :class:`KbError` if ``kb/entries/`` (or an entry file) can't be
    read. An empty result is success, not an error.
    """
    root = root if root is not None else DEFAULT_KB_ROOT
    needle = query.lower()
    matches = [
        _summary(entry)
        for entry in (_load_entry(path) for path in _entry_paths(root))
        if _matches(entry, needle)
    ]
    matches.sort(key=lambda entry: entry["id"] or "")
    return {
        "schema_version": 1,
        "query": query,
        "count": len(matches),
        "entries": matches,
    }


def _entry_errors(
    path: Path, validator: jsonschema.protocols.Validator
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

    return entry, errors


def validate_entries(root: Path | None = None) -> dict[str, Any]:
    """Validate every entry: well-formed JSON, conforms to
    ``kb/schema/entry.schema.json``, and ``id`` matches its filename stem.

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

    results = []
    for path in _entry_paths(root):
        _entry, errors = _entry_errors(path, validator)
        results.append({"id": path.stem, "valid": not errors, "errors": errors})

    return {
        "schema_version": 1,
        "valid": all(result["valid"] for result in results),
        "entry_count": len(results),
        "entries": results,
    }
