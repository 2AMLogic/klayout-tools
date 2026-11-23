"""Manifest-declared sweep coverage for signoff spec rows (items 5 and 6)."""

from __future__ import annotations

from typing import Any, TypeGuard


def label_list(value: Any) -> TypeGuard[list[str]]:
    """A list of opaque, non-empty labels. An extent additionally needs a member."""
    return isinstance(value, list) and all(isinstance(s, str) and s for s in value)


def has_covers(entry: Any) -> bool:
    return isinstance(entry, dict) and "covers" in entry


def invalid_covers(
    members: list[tuple[dict[str, Any], list[str] | None]],
    spec_rows: list[dict[str, Any]] | None,
) -> str | None:
    """Check each member's assertions before grading or running its citation."""
    extents = {
        row["id"]: set(row["extent"]) for row in spec_rows or [] if "extent" in row
    }
    for index, (spec, rows) in enumerate(members, 1):
        if "covers" not in spec:
            continue
        covers = spec["covers"]
        if not isinstance(covers, dict):
            return f"member {index} 'covers' must map row ids to lists of slices"
        for row, labels in covers.items():
            if row not in (rows or []) or row not in extents:
                return f"member {index} 'covers' needs a cited extent row: {row!r}"
            if not label_list(labels) or not extents[row].issuperset(labels):
                return f"member {index} 'covers' must list declared slices of {row!r}"
    return None


def _row_claims(
    graded: list[Any], credited: dict[int, tuple[list[str] | None, bool]]
) -> dict[str, dict[str | None, str]]:
    """Row -> slice -> provenance. None stands for an unsliced row assertion."""
    claims: dict[str, dict[str | None, str]] = {}
    for spec, rows, grade in graded:
        member_rows, derived = credited.get(id(spec), (rows, False))
        if grade[0] != "met" and not derived:
            continue
        # A failed simulation can prove an unsliced row through its checked
        # measurements. Only a passing citation contributes configuration slices.
        covers = spec.get("covers", {}) if grade[0] == "met" else {}
        for row in member_rows or []:
            sources = claims.setdefault(row, {})
            for label in covers.get(row, [None]):
                if derived or label not in sources:
                    sources[label] = "derived" if derived else "asserted"
    return claims


def row_credits(
    graded: list[Any],
    credited: dict[int, tuple[list[str] | None, bool]],
    rows: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any], list[dict[str, Any]]]:
    """Fully covered rows and their provenance, slice unions, and partial rows.

    A tagged measurement authenticates the row, while the citation declares the
    configuration labels. Preserve declaration order in every reported list.
    """
    claims = _row_claims(graded, credited)
    row_sources: dict[str, str] = {}
    slices: dict[str, Any] = {}
    partial: list[dict[str, Any]] = []
    for row in rows:
        row_id = row["id"]
        sources = claims.get(row_id, {})
        if "extent" not in row:
            if None in sources:
                row_sources[row_id] = sources[None]
            continue
        extent = row["extent"]
        covered = [label for label in extent if label in sources]
        missing = [label for label in extent if label not in sources]
        slices[row_id] = {"covered": covered, "missing": missing}
        if not missing:
            row_sources[row_id] = (
                "derived"
                if all(sources[label] == "derived" for label in extent)
                else "asserted"
            )
        elif row["gap"] is None and row["justified_gap"] is None:
            partial.append({"id": row_id, "extent": extent, **slices[row_id]})
    return row_sources, slices, partial


def print_partial_rows(items: list[dict] | None) -> None:
    """Append partial sweep rows from the graded items, including their partitions."""
    for item in items or []:
        for row in (item.get("detail") or {}).get("partial_rows", []):
            label = f"item {item['id']}"
            if item.get("partition"):
                label += f".{item['partition']}"
            print(
                f"[PARTIAL] {row['id']} ({label}): "
                f"{len(row['covered'])}/{len(row['extent'])} slices covered, "
                f"missing {', '.join(row['missing'])}"
            )
