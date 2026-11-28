"""``klt extract --spef`` block (issue #5196, stage 5).

Translate the extracted parasitic report into a SPEF file, reconcile its
names with the routed DEF, and append warnings to the extraction report.
Return the output path when the caller requests SPEF.

The helpers
it calls that still live in ``extract.py`` are imported inside the function
body: ``extract.py`` imports this module at its top level, so a module-level
``from .extract import ...`` would be a circular import. (It lives beside
``extract_spef.py`` rather than in it because that module deliberately never
reaches back into ``extract.py``.)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .extract_base import spice_safe_net_name
from .extract_spef import _unescape_spice_safe_net_name

if TYPE_CHECKING:
    import klayout.db as kdb


def _observed_def_renames(
    circuit: kdb.Circuit | None,
    connections: Mapping[str, Any] | None,
) -> list[tuple[str, str, str]]:
    """Match raw FinFET circuit names to their unique spellings in the report."""
    if circuit is None or not connections or not any("." in key for key in connections):
        return []
    names = [net.expanded_name() for net in circuit.each_net()]
    spellings = [
        _unescape_spice_safe_net_name(spice_safe_net_name(name)) for name in names
    ]
    counts = Counter(spellings)
    return [
        (circuit.name, original, spelling)
        for original, spelling in zip(names, spellings, strict=True)
        if "." in original and original in connections and counts[spelling] == 1
    ]


def _unmatched_def_names(
    original: Mapping[str, Any], remapped: Mapping[str, Any]
) -> dict[str, list[str]]:
    """Group remaining dotted DEF keys by their possible extracted spelling."""
    unmatched: dict[str, list[str]] = defaultdict(list)
    for key in original:
        if "." in key and key in remapped:
            name = _unescape_spice_safe_net_name(spice_safe_net_name(key))
            unmatched[name].append(key)
    return unmatched


def _spef_missing_conn_warnings(
    parasitics_report: Mapping[str, Any],
    original: Mapping[str, Any] | None,
    remapped: Mapping[str, Any] | None,
) -> list[str]:
    """Name the DEF and extracted spellings when instance-pin lookup fails."""
    if not remapped:
        return []
    counts = Counter(
        _unescape_spice_safe_net_name(net["net"]) for net in parasitics_report["nets"]
    )
    unmatched = _unmatched_def_names(original or {}, remapped)
    missing = sorted(
        name
        for name, count in counts.items()
        if (remapped.get(name) and count > 1)
        or (name in unmatched and (not remapped.get(name) or count > 1))
    )
    if not missing:
        return []
    examples = [
        f"DEF {', '.join(unmatched[name])} -> extracted {name}"
        if name in unmatched
        else name
        for name in missing[:5]
    ]
    if len(missing) > len(examples):
        examples.append(f"+{len(missing) - len(examples)} more")
    return [
        f"{len(missing)} *D_NET lookup(s) could not uniquely match the extracted net "
        f"to its DEF instance pins ({', '.join(examples)}). Duplicated names cannot "
        "identify a single net island. Downstream STA may report incomplete annotation."
    ]


def write_spef_block(
    *,
    circuit: kdb.Circuit | None,
    spef_output: Any,
    parasitics_report: Any,
    def_net_connections: Any,
    dotted_renames: Any,
    top_cell_name: Any,
    nets: Any,
    def_pins: Any,
    declared_pins: Any,
    warnings: Any,
) -> str | None:
    import os

    from ._provenance import klt_version
    from .extract import (
        _remap_def_net_connections,
        _spef_warnings,
    )
    from .extract_spef import ExtractError, _write_spef

    # `--spef` (issue #948): translate the just-built `parasitics_report`
    # into a SPEF file at `spef_output` -- see `_write_spef`'s docstring for
    # the exact shape and its net-name-only correlation scope. Only reached
    # when `parasitics_report is not None` (guaranteed above: `spef_output`
    # given requires `parasitics=True`, which is exactly when
    # `parasitic_nets is not None`).
    spef_path: str | None = None
    if spef_output is not None:
        assert parasitics_report is not None
        spef_path = spef_output
        spef_out_dir = os.path.dirname(os.path.abspath(spef_path))
        try:
            os.makedirs(spef_out_dir, exist_ok=True)
        except OSError as exc:
            raise ExtractError(
                f"cannot create output directory {spef_out_dir}: {exc}"
            ) from exc
        renames = dotted_renames or _observed_def_renames(circuit, def_net_connections)
        spef_conn_map = _remap_def_net_connections(def_net_connections, renames)
        spef_skipped_nets = _write_spef(
            spef_path,
            design_name=top_cell_name,
            klt_version=klt_version(),
            parasitics_report=parasitics_report,
            port_names=(entry["name"] for entry in nets if entry["pin"]),
            # Issue #961: real cell-instance `*CONN`/`*RES` correlation from
            # the routed DEF's own `NETS` section -- `None` (the default)
            # falls back to the pre-#961 port-only behavior.
            net_instance_pins=spef_conn_map,
        )
        warnings.extend(
            _spef_missing_conn_warnings(
                parasitics_report, def_net_connections, spef_conn_map
            )
        )
        unmatched = _unmatched_def_names(def_net_connections or {}, spef_conn_map or {})
        unknown_nets = [name for name in spef_skipped_nets if name not in unmatched]
        warnings.extend(
            _spef_warnings(def_net_connections, def_pins, declared_pins, unknown_nets)
        )
    return spef_path
