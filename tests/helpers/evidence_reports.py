"""Shared fixtures for the two ``klt evidence canonicalize`` test modules
(issue #5541): ``test_evidence_canonicalize.py`` (registry, removal,
determinism, CLI) and ``test_evidence_canonicalize_rerun_equivalence.py``
(the ``--rerun`` relationship). Split so neither module carries the whole
suite past the per-file size ratchet's 500-line limit.
"""

from __future__ import annotations

import json
from typing import Any

from klayout_tools import evidence_canonicalize as ec
from klayout_tools._report_verify import diff_verdict_fields

#: The ``--kind`` choices, as a list for ``pytest.mark.parametrize``.
KINDS = list(ec.CANONICAL_KINDS)

_PROVENANCE: dict[str, Any] = {
    "klt_version": "0.9.1",
    "klt_git_commit": "deadbeef",
    "klt_reinstall_spec": "klayout-tools==0.9.1",
    "klayout_version": "0.29.0",
    "pdk": {
        "variant": "sky130A",
        "version": "v1",
        "commit": "cafe",
        "linked_trees": ["/opt/pdk"],
        "ambiguous_sources": ["/opt/pdk", "/usr/share/pdk"],
        "source": "/opt/pdk",
    },
}


def base_report(kind: str) -> dict[str, Any]:
    """A minimal report of ``kind`` carrying every field class: verdict,
    excluded-by-path, and -- for a kind with a canonicalizer -- run-scoped
    bookkeeping both top-level and nested inside a verdict object."""
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "clean",
        "provenance": json.loads(json.dumps(_PROVENANCE)),
        "timing": {"elapsed_s": 1.5},
    }
    if kind in {"drc", "erc"}:
        report |= {
            "file": {"path": "block.gds", "scope": "repo"},
            "violation_count": 0,
            "violations": [],
        }
    elif kind == "lvs":
        report |= {
            "mismatch_count": 0,
            "mismatches": [],
            "environment": {
                "engine_version": "netgen 1.5",
                "layout_sha256": "aa",
                "reference_sha256": "bb",
                "extracted_netlist": "/tmp/run-1/extracted.spice",
                "netgen_binary": "/usr/local/bin/netgen",
                "klayout_binary": "/usr/local/bin/klayout",
            },
        }
    elif kind == "extract":
        report |= {
            "device_count": 1,
            "net_count": 2,
            "devices": [
                {"name": "M1", "class": "nmos", "nets": {"S": "\\$1", "D": "vout"}}
            ],
            "nets": [
                {"name": "\\$1", "net_id": 7, "device_count": 1},
                {"name": "vout", "net_id": 2, "device_count": 1},
            ],
        }
    elif kind == "synthesize":
        report |= {
            "engine": "yosys",
            "engine_version": "yosys 0.38",
            "instance_count": 12,
            "area_um2": 34.5,
            "run_id": "11111111-1111-1111-1111-111111111111",
            "netlist_path": ".klt/synthesize/1111/netlist.v",
            "script_path": ".klt/synthesize/1111/run.ys",
            "equivalence": {
                "status": "equivalent",
                "artifacts": {"script_path": ".klt/synthesize/1111/equiv.ys"},
            },
            "macros": [{"name": "sram", "lef": "sram.lef", "blackbox_path": "bb.v"}],
        }
    elif kind == "place-and-route":
        report |= {
            "engine": "openroad",
            "engine_version": "openroad 2.0",
            "def_path": ".klt/place-and-route/block.def",
            "wns_ns": -0.1,
            "engine_logs": [{"invocation_id": "abc", "directory": "/tmp/abc"}],
            "stages": {"detailed_route": {"engine_logs": [{"invocation_id": "d"}]}},
        }
    elif kind == "equiv":
        report |= {
            "engine": "yosys",
            "engine_version": "yosys 0.38",
            "elapsed_s": 2.25,
            "resume": {"resumed_stage": "miter"},
            "yosys_binary": "/usr/local/bin/yosys",
            "counterexample": {
                "found": False,
                "simulation": {
                    "iverilog_binary": "/usr/bin/iverilog",
                    "vvp_binary": "/usr/bin/vvp",
                },
            },
        }
    return report


def rerun_drift(
    kind: str, committed: dict[str, Any], fresh: dict[str, Any]
) -> list[dict[str, Any]]:
    """What ``klt <kind> --rerun`` would report for this pair (the static
    view, no pair-dependent extras)."""
    spec = ec.kind_spec(kind)
    view = spec.canonicalize or (lambda report: report)
    return diff_verdict_fields(view(committed), view(fresh), exclude=spec.exclude)


def set_path(report: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Set ``path`` in ``report``, creating intermediate objects."""
    node = report
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value
