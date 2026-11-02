"""SPICE-legal names for gate-level Verilog modules declared under an escaped
identifier (issue #4810), e.g. Yosys's ``\\$paramod\\child\\W=s32'd64``."""

from __future__ import annotations

import hashlib
import re

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def module_name_map(names: list[str]) -> dict[str, str]:
    """``{original: mapped}`` for each declared module name that is not already
    SPICE-legal: characters outside ``[A-Za-z0-9_]`` become ``_``; when that
    collides with another declared name's sanitized form, a short stable hash
    of the original is appended. Names needing no mapping are omitted."""
    candidates = {name: _UNSAFE_RE.sub("_", name) for name in names}
    counts: dict[str, int] = {}
    for candidate in candidates.values():
        counts[candidate] = counts.get(candidate, 0) + 1
    mapping: dict[str, str] = {}
    for name, candidate in candidates.items():
        if candidate == name:
            continue
        if counts[candidate] > 1:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
            candidate = f"{candidate}_{digest}"
        mapping[name] = candidate
    return mapping


def apply_module_names(modules: list[dict[str, object]]) -> dict[str, str]:
    """Rename, in place, each parsed module and every instantiation of it per
    :func:`module_name_map`; returns that mapping."""
    mapping = module_name_map([str(m["name"]) for m in modules])
    for module in modules if mapping else ():
        module["name"] = mapping.get(str(module["name"]), module["name"])
        for inst in module["instances"]:  # type: ignore[attr-defined]
            inst["cell"] = mapping.get(inst["cell"], inst["cell"])
    return mapping


def reference_module_names(path: str, form: str) -> dict[str, dict[str, str]]:
    """``{"reference_module_names": {original: SPICE name}}`` for the escaped
    modules of a gate-level-verilog reference file (issue #4810); ``{}`` for
    every other form or when none was mapped, so it splats into the report."""
    if form != "gate-level-verilog":
        return {}
    from .verilog_netlist import _parse_with_names

    with open(path, encoding="utf-8") as handle:
        names = _parse_with_names(handle.read())[1]
    return {"reference_module_names": names} if names else {}
