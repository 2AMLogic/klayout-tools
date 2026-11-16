"""``klt extract --spef`` block (issue #5196, stage 5).

Moved verbatim out of ``run_extract``: translate the just-built
``parasitics_report`` into a SPEF file at ``spef_output`` and append the
SPEF-related warnings. Returns ``spef_path`` (``None`` when ``--spef`` was not
given).

``warnings`` is mutated in place, exactly as the inline block did. The helpers
it calls that still live in ``extract.py`` are imported inside the function
body: ``extract.py`` imports this module at its top level, so a module-level
``from .extract import ...`` would be a circular import. (It lives beside
``extract_spef.py`` rather than in it because that module deliberately never
reaches back into ``extract.py``.)
"""

from __future__ import annotations

from typing import Any


def write_spef_block(
    *,
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

    from ._provenance import _klt_version
    from .extract import (
        _remap_def_net_connections,
        _spef_missing_conn_warnings,
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
        spef_conn_map = _remap_def_net_connections(def_net_connections, dotted_renames)
        spef_skipped_nets = _write_spef(
            spef_path,
            design_name=top_cell_name,
            klt_version=_klt_version(),
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
        warnings.extend(
            _spef_warnings(
                def_net_connections, def_pins, declared_pins, spef_skipped_nets
            )
        )
    return spef_path
