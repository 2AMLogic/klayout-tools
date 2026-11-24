"""Shared ``joint_grid`` fixtures (issue #6969).

Originally defined in ``tests/test_signoff_joint_rows.py``; the
per-spec-row table tests split into
``tests/test_signoff_joint_rows_table.py`` (a new test file stays under
the 500-line ceiling) now share these definitions.
"""

from __future__ import annotations

import json

from klayout_tools.signoff import build_tier_report

_PROCESSES = ("tt", "ss", "ff")
_SUPPLIES = (1.62, 1.8, 1.98)
_TEMPERATURES = (-40, 27, 125)

_GRID = {
    "process": list(_PROCESSES),
    "supply_v": {"vdd": list(_SUPPLIES)},
    "temperature_c": list(_TEMPERATURES),
}

SIM_PASS_ENVELOPE = {
    "schema_version": 1,
    "netlist": "design.spice",
    "status": "pass",
    "corner_count": 3,
    "passed": 3,
    "failed": 0,
    "errored": 0,
    "environment": {},
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": {"name": "sky130.lib.spice", "content_hash": "sha256:models"},
        "input": {"content_hash": "sha256:netlistA", "role": "netlist"},
    },
    "measurements": [],
    "corners": [],
}


def _write(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _grid_envelope(
    *,
    probe=True,
    probe_limits=None,
    drop=None,
    fail=None,
    monte_carlo=False,
) -> dict:
    """One `klt sim` envelope over the full 3x3x3 PVT grid. ``drop``
    removes a corner, ``fail`` marks one ``fail``, ``probe`` attaches the
    row's condition probe (a ``spec_row``-tagged, ``limits``-bearing,
    passing rolled-up measurement), ``monte_carlo`` marks every corner a
    sample."""
    corners = []
    for process in _PROCESSES:
        for supply in _SUPPLIES:
            for temp in _TEMPERATURES:
                if drop == (process, supply, temp):
                    continue
                corner = {
                    "corner_id": f"{process}/{supply:.3f}V/{temp}C",
                    "process": process,
                    "supply_v": {"vdd": supply},
                    "temperature_c": temp,
                    "status": "fail" if fail == (process, supply, temp) else "pass",
                }
                if monte_carlo:
                    corner["monte_carlo"] = {"sample_index": 0}
                corners.append(corner)
    measurements = []
    if probe:
        entry = {"name": "vdd_applied", "spec_row": "pvt_grid", "status": "pass"}
        if probe_limits is not False:
            entry["limits"] = probe_limits or {"min": 1.6}
        measurements.append(entry)
    return {
        **SIM_PASS_ENVELOPE,
        "corner_count": len(corners),
        "passed": len(corners),
        "measurements": measurements,
        "corners": corners,
    }


def _item5(evidence, spec_rows=None, **overrides) -> dict:
    manifest = {"block": "demo-block", "kind": "analog", "evidence": evidence}
    if spec_rows is not None:
        manifest["spec_rows"] = {"5": spec_rows}
    manifest.update(overrides)
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)


def _joint_members(*paths, grid=None) -> dict:
    return {"members": list(paths), **(grid or _GRID)}
