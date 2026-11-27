"""Derive paired nominal/control CSV populations and record their run provenance."""

from __future__ import annotations

from typing import Any

from .yield_analysis import YieldError, derive_sample_set_from_csv
from .yield_provenance import _provenance_from


def derive_csv_samples(
    csv_path: str,
    *,
    group_by: str,
    value: str,
    status_col: str | None = None,
    pass_value: str | None = None,
    draw_col: str | None = None,
    unit: str | None = None,
    limits: dict[str, float] | None = None,
    negative_control: str | None = None,
    description: str | None = None,
    seed: int | None = None,
    rng: str | None = None,
    draws_per_population: int | None = None,
) -> dict[str, Any]:
    if description is not None and negative_control is None:
        raise YieldError("--description requires --negative-control")
    columns: dict[str, Any] = {
        "group_by": group_by,
        "value": value,
        "status_col": status_col,
        "pass_value": pass_value,
        "draw_col": draw_col,
    }
    provenance = {
        key: item
        for key, item in (
            ("seed", seed),
            ("rng", rng),
            ("draws_per_population", draws_per_population),
        )
        if item is not None
    }
    document = derive_sample_set_from_csv(csv_path, unit=unit, limits=limits, **columns)
    if provenance:
        document["provenance"] = _provenance_from(provenance, csv_path)
    if negative_control is None:
        return document
    control = derive_sample_set_from_csv(negative_control, **columns)
    groups = {m["name"]: m for m in control["measurements"]}
    for measurement in document["measurements"]:
        name = measurement["name"]
        if name not in groups:
            raise YieldError(
                f"negative control CSV '{negative_control}' lacks group {name!r}"
            )
        population = groups[name]
        measurement["negative_control"] = {
            "samples": population["samples"],
            "errored": population["errored"],
            "failed_unmeasurable": 0,
            "censored": 0,
            "description": description,
        }
    evidence = control["derivation"]["csv"]
    document["derivation"]["negative_control"] = {
        "path": evidence["path"],
        "content_hash": evidence["content_hash"],
        "rows": evidence["rows"],
        "description": description,
    }
    return document
