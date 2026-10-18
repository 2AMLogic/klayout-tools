#!/usr/bin/env python3
"""Regenerate the frozen layout-benchmark corpus (issue #3025, epic #2252).

Writes ``benchmarks/design-agent/layout-corpus/<task>/``: ``netlist.spice`` (the
frozen LVS reference), ``recipe.json`` (the ``klt gen`` / ``gen-compose`` steps
that reach a valid layout), ``reference.gds`` (that recipe's output), the two
negative controls, and ``task.json`` (hashes bound). Also writes
``exclusions.json``. The committed files are authoritative -- this script only
documents and reproduces how they were made; the credential-free gate is
``design_agent_benchmark.py validate-layout --corpus``.

Negative controls:

* ``neg-drc.gds`` -- the reference plus one 0.05 um met1 sliver clear of the
  cell: a DRC width violation that adds no device and no connection, so LVS
  still matches.
* ``neg-lvs.gds`` -- the same recipe with one device resized: legal geometry
  (DRC-clean by construction) but a different circuit, so LVS mismatches.

Usage (from the repo root, sky130A PDK resolvable):

    uv run python scripts/build_design_agent_layout_corpus.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import design_agent_benchmark_layout as layout  # noqa: E402

CORPUS = layout.CORPUS_DIR
REPO = layout.REPO_ROOT
PDK = "sky130A"
MET1 = (68, 20)
SLIVER_UM = 0.05


def _mos(step_id: str, cell: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": step_id,
        "verb": "gen",
        "generator": "mos_array",
        "cell_name": cell,
        "params": {"rows": 1, "cols": 1, "dummy": 0, **params},
    }


def _ota_recipe(tail_w_um: float) -> dict[str, Any]:
    return {
        "schema": layout.RECIPE_SCHEMA,
        "pdk": PDK,
        "steps": [
            _mos("tail", "ota_tail", {"w_um": tail_w_um, "l_um": 1}),
            {
                "id": "diffpair",
                "verb": "gen",
                "generator": "diff_pair",
                "cell_name": "ota_diffpair",
                "params": {
                    "w_um": 8,
                    "l_um": 0.5,
                    "mirror": False,
                    "splits": 1,
                    "add_guard_ring": False,
                },
            },
            {
                "id": "mirror",
                "verb": "gen",
                "generator": "diff_pair",
                "cell_name": "ota_mirror",
                "params": {
                    "w_um": 6,
                    "l_um": 1,
                    "mirror": True,
                    "splits": 1,
                    "add_guard_ring": False,
                },
            },
            {
                "id": "compose",
                "verb": "gen-compose",
                "cell_name": "five_transistor_ota",
                "request": {
                    "blocks": [
                        {"id": "tail", "step": "tail"},
                        {"id": "diffpair", "step": "diffpair"},
                        {"id": "mirror", "step": "mirror"},
                    ],
                    "placement": {
                        "strategy": "row",
                        "order": ["tail", "diffpair", "mirror"],
                        "spacing_um": 1.0,
                    },
                    "connectivity": [
                        {
                            "net": "TAIL_A",
                            "pins": [
                                {"block": "tail", "port": "U0_D"},
                                {"block": "diffpair", "port": "Q1_1_S"},
                            ],
                        },
                        {
                            "net": "TAIL_B",
                            "pins": [
                                {"block": "tail", "port": "U0_D"},
                                {"block": "diffpair", "port": "Q2_1_S"},
                            ],
                        },
                        {
                            "net": "N1",
                            "pins": [
                                {"block": "diffpair", "port": "Q1_1_D"},
                                {"block": "mirror", "port": "M1_1_S"},
                            ],
                        },
                        {
                            "net": "VOUT",
                            "pins": [
                                {"block": "diffpair", "port": "Q2_1_D"},
                                {"block": "mirror", "port": "M2_1_S"},
                            ],
                        },
                    ],
                    "routing": {"layer_role": "metal", "width_um": 0.17},
                },
            },
        ],
    }


def _single(generator: str, cell: str, params: dict[str, Any]) -> dict[str, Any]:
    step = (
        _mos("cell", cell, params)
        if generator == "mos_array"
        else {
            "id": "cell",
            "verb": "gen",
            "generator": generator,
            "cell_name": cell,
            "params": params,
        }
    )
    return {"schema": layout.RECIPE_SCHEMA, "pdk": PDK, "steps": [step]}


_MOS_NOTE = (
    "The generators draw label-free cells, so only the substrate is a top-level pin; "
    "gate/source/drain nets are internal and LVS compares topology and device W/L."
)

# id -> (title, io_nodes, description, recipe, perturbed recipe (LVS control), netlist)
CELLS: dict[str, dict[str, Any]] = {
    "nfet-unit": {
        "title": "Single NMOS unit",
        "spec_class": "other",
        "io_nodes": ["D", "G", "S", "VSS"],
        "description": "One sky130 core NMOS, W/L = 2/0.5 um. " + _MOS_NOTE,
        "recipe": _single("mos_array", "nfet_unit", {"w_um": 2, "l_um": 0.5}),
        "lvs_mutant": _single("mos_array", "nfet_unit", {"w_um": 3, "l_um": 0.5}),
        "netlist": ".subckt nfet_unit vsubs\nM1 d g s vsubs nfet L=0.5U W=2U\n.ends\n",
    },
    "pfet-unit": {
        "title": "Single PMOS unit",
        "spec_class": "other",
        "io_nodes": ["D", "G", "S", "VDD"],
        "description": "One sky130 core PMOS in its own n-well, W/L = 2/0.5 um. "
        + _MOS_NOTE,
        "recipe": _single(
            "mos_array", "pfet_unit", {"w_um": 2, "l_um": 0.5, "flavor": "pfet"}
        ),
        "lvs_mutant": _single(
            "mos_array", "pfet_unit", {"w_um": 3, "l_um": 0.5, "flavor": "pfet"}
        ),
        "netlist": ".subckt pfet_unit\nM1 d g s b pfet L=0.5U W=2U\n.ends\n",
    },
    "diff-pair": {
        "title": "NMOS differential pair",
        "spec_class": "other",
        "io_nodes": ["D1", "D2", "G1", "G2", "S", "VSS"],
        "description": "Two matched sky130 NMOS, W/L = 8/0.5 um each. " + _MOS_NOTE,
        "recipe": _single(
            "diff_pair",
            "diffpair_cell",
            {
                "w_um": 8,
                "l_um": 0.5,
                "mirror": False,
                "splits": 1,
                "add_guard_ring": False,
            },
        ),
        "lvs_mutant": _single(
            "diff_pair",
            "diffpair_cell",
            {
                "w_um": 10,
                "l_um": 0.5,
                "mirror": False,
                "splits": 1,
                "add_guard_ring": False,
            },
        ),
        "netlist": (
            ".subckt diffpair_cell vsubs\n"
            "M1 d1 g1 s1 vsubs nfet L=0.5U W=8U\n"
            "M2 d2 g2 s2 vsubs nfet L=0.5U W=8U\n"
            ".ends\n"
        ),
    },
    "five-transistor-ota": {
        "title": "Five-transistor OTA (composed)",
        "spec_class": "ota",
        "io_nodes": ["INP", "INN", "OUT", "VDD", "VSS"],
        "description": (
            "The design-pipeline 5T OTA composed from a tail mos_array and two "
            "diff_pair blocks with klt gen-compose (examples/design-pipeline). "
            "Only the nets the composer routes (N1, TAIL_NODE, VOUT) and the "
            "substrate are top-level pins; gates and unrouted terminals are "
            "floating internal nets."
        ),
        "recipe": _ota_recipe(10),
        "lvs_mutant": _ota_recipe(12),
        "netlist": (
            ".subckt five_transistor_ota N1 TAIL_NODE VOUT vsubs\n"
            "M1 N1        g1 flt1 vsubs nfet L=1U   W=6U\n"
            "M2 TAIL_NODE g2 N1    vsubs nfet L=0.5U W=8U\n"
            "M3 flt3      g3 TAIL_NODE vsubs nfet L=1U   W=10U\n"
            "M4 flt4      g4 VOUT  vsubs nfet L=1U   W=6U\n"
            "M5 TAIL_NODE g5 VOUT  vsubs nfet L=0.5U W=8U\n"
            ".ends\n"
        ),
    },
}

CURRENT_MIRROR_RECIPE = {
    "schema": layout.RECIPE_SCHEMA,
    "pdk": PDK,
    "steps": [
        _mos("unit", "mirror_unit", {"w_um": 2, "l_um": 0.5}),
        {
            "id": "compose",
            "verb": "gen-compose",
            "cell_name": "nfet_current_mirror",
            "request": {
                "blocks": [
                    {"id": "ref", "step": "unit"},
                    {"id": "out", "step": "unit"},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["ref", "out"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "IREF",
                        "pins": [
                            {"block": "ref", "port": "U0_D"},
                            {"block": "ref", "port": "U0_G"},
                            {"block": "out", "port": "U0_G"},
                        ],
                    },
                    {
                        "net": "VSS",
                        "pins": [
                            {"block": "ref", "port": "U0_S"},
                            {"block": "out", "port": "U0_S"},
                        ],
                    },
                ],
                "routing": {"layer_role": "metal", "width_um": 0.17},
            },
        },
    ],
}


def _write_json(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_sliver(src: Path, dst: Path, top: str) -> None:
    """Copy ``src`` to ``dst`` plus a sub-min-width met1 rectangle beside the cell."""
    import klayout.db as pya

    lay = pya.Layout()
    lay.read(str(src))
    tops = [c.name for c in lay.top_cells()]
    if tops != [top]:
        sys.exit(f"{src}: expected the single top cell {top!r}, found {tops}")
    cell = lay.cell(top)
    box = cell.bbox()
    layer = lay.layer(*MET1)
    dbu = lay.dbu
    x0 = box.right + int(2.0 / dbu)
    cell.shapes(layer).insert(
        pya.Box(x0, box.bottom, x0 + int(SLIVER_UM / dbu), box.bottom + int(2.0 / dbu))
    )
    options = pya.SaveLayoutOptions()
    options.format = "GDS2"
    options.gds2_write_timestamps = False  # byte-reproducible stream
    lay.write(str(dst), options)


_RULE_EVIDENCE = {
    "frozen_netlist": (
        "netlist.spice is a hand-written reference, hashed in task.json; the "
        "scorer reads it from the repo, never from an agent sandbox."
    ),
    "reference_layout_valid": (
        "reference.gds is the recipe's output; the corpus gate re-runs klt drc "
        "(sky130 deck: clean) and klt lvs (match) on it."
    ),
    "generator_reachable": (
        "recipe.json lists only klt gen / klt gen-compose steps; the gate re-runs "
        "it and requires a DRC-clean, LVS-matching layout."
    ),
    "negative_controls": (
        "neg-drc.gds fails only DRC (met1 width sliver) and neg-lvs.gds fails only "
        "LVS (resized device); the gate checks each check fires alone."
    ),
    "open_pdk": "sky130A via open_pdks; no proprietary data.",
}


def _eligibility(cell: str) -> dict[str, Any]:
    return {
        "rules": dict(_RULE_EVIDENCE),
        "arms": {
            "draw-only": (
                f"{cell} is a bounded layout of a few devices with a "
                "netlist-defined target; klt draw primitives can express a legal "
                "layout of it, and the gate scores it identically."
            ),
            "gen-only": (
                "Reachable with klt gen / gen-compose alone: recipe.json is the "
                "existence proof and uses no draw verb."
            ),
            "gen+refine": (
                "Seeded from the gen-only result (protocol seeding decision); every "
                "refinement edit is expressible with klt draw (base_gds / erase_um) "
                "or gen-compose."
            ),
        },
    }


def build() -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    for task_id, cell in CELLS.items():
        out = CORPUS / task_id
        out.mkdir(exist_ok=True)
        (out / "netlist.spice").write_text(cell["netlist"])
        _write_json(out / "recipe.json", cell["recipe"])
        top = cell["recipe"]["steps"][-1]["cell_name"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            built = layout.run_recipe(cell["recipe"], tmp_path / "ref")
            (out / "reference.gds").write_bytes(built.read_bytes())
            mutant = layout.run_recipe(cell["lvs_mutant"], tmp_path / "mut")
            (out / "neg-lvs.gds").write_bytes(mutant.read_bytes())
        _add_sliver(out / "reference.gds", out / "neg-drc.gds", top)
        task = {
            "schema": "klt.design_agent_benchmark.layout_task/1",
            "id": task_id,
            "title": cell["title"],
            "pdk": "sky130",
            "description": cell["description"],
            "block_spec": {
                "spec_class": cell["spec_class"],
                "io_nodes": cell["io_nodes"],
                "target_specs": None,
            },
            "frozen_netlist": _ref(out, "netlist.spice"),
            "reference_layout": {**_ref(out, "reference.gds"), "cell_name": top},
            "generator_recipe": _ref(out, "recipe.json"),
            "negative_controls": [
                {
                    "id": "met1-width",
                    **_ref(out, "neg-drc.gds"),
                    "expected_failure": "drc",
                },
                {
                    "id": "resized-device",
                    **_ref(out, "neg-lvs.gds"),
                    "expected_failure": "lvs",
                },
            ],
            "agent_inputs": [
                f"benchmarks/design-agent/layout-corpus/{task_id}/netlist.spice"
            ],
            "eligibility": _eligibility(task_id),
            "notes": None,
        }
        _write_json(out / "task.json", task)

    excl_dir = CORPUS / "exclusions"
    excl_dir.mkdir(exist_ok=True)
    recipe_path = excl_dir / "current-mirror.recipe.json"
    _write_json(recipe_path, CURRENT_MIRROR_RECIPE)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            layout.run_recipe(copy.deepcopy(CURRENT_MIRROR_RECIPE), Path(tmp))
        except layout.RecipeError as exc:
            evidence = str(exc)
        else:
            sys.exit("current-mirror recipe reached a layout; drop the exclusion")
    _write_json(
        CORPUS / "exclusions.json",
        {
            "schema": "klt.design_agent_benchmark.layout_exclusions/1",
            "exclusions": [
                {
                    "id": "current-mirror",
                    "title": "NMOS current mirror",
                    "reason": "generator_reachable",
                    "detail": (
                        "klt gen-compose cannot wire a diode-connected mirror from "
                        "mos_array blocks (bare-poly gate pins need gate_contact and "
                        "the shared-net backbone would cross a block's own pad), so "
                        "the gen-only arm has no recipe. Excluded from all three "
                        "arms. First failure: " + evidence[:400]
                    ),
                    "recipe": {
                        "path": str(recipe_path.relative_to(REPO)),
                        "sha256": _sha(recipe_path),
                    },
                }
            ],
        },
    )


def _ref(out: Path, name: str) -> dict[str, str]:
    return {"path": str(out.relative_to(REPO) / name), "sha256": _sha(out / name)}


if __name__ == "__main__":
    build()
