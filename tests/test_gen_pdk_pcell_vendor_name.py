"""`--pdk-pcell` child-name pinning tests (issue #5933).

Split out of `test_gen_pdk_pcell.py` when that file hit its trusted
file-size ceiling (1084 lines, `file-size-baseline.json`): the ratchet is
monotonic, so growth moved here instead. The toy-PDK fixtures live in
`test_gen_pdk_pcell`; see its module docstring for how they are fabricated.
"""

from klayout_tools.pdk_pcell import generate_pdk_pcell
from test_gen_pdk_pcell import _toy_pdk


def _read_hierarchy(path):
    """``{cell_name: (is_top, sorted child cell names)}`` read from the stream."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(path))
    top_names = {c.name for c in layout.top_cells()}
    return {
        cell.name: (
            cell.name in top_names,
            sorted(layout.cell(i).name for i in cell.each_child_cell()),
        )
        for cell in layout.each_cell()
    }


def test_generate_pdk_pcell_child_keeps_vendor_name_not_core_suffix(tmp_path):
    """Pins ``docs/cli/gen.md``'s ``--cell-name`` clause (issue #5933).

    Unlike a built-in generator (whose child is ``<cell-name>__core``), the
    ``--pdk-pcell`` child keeps the vendor PCell's own name regardless of
    ``--cell-name``. If this ever changes, the stream's structure names change
    -- update the docs clause and add a CHANGELOG entry in the same PR.
    """
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "named.gds"

    generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "options": {"cell_name": "judgebar", "output": str(output)},
        }
    )

    hierarchy = _read_hierarchy(output)
    assert hierarchy == {
        "judgebar": (True, [cell]),
        cell: (False, []),
    }
    assert not any(name.endswith("__core") for name in hierarchy)


def test_generate_pdk_pcell_child_name_is_independent_of_cell_name(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)
    children = set()
    for cell_name in ("alpha", "beta"):
        output = tmp_path / f"{cell_name}.gds"
        generate_pdk_pcell(
            {
                "pdk_pcell": f"{library}/{cell}",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "options": {"cell_name": cell_name, "output": str(output)},
            }
        )
        (_is_top, kids) = _read_hierarchy(output)[cell_name]
        children.update(kids)
    # Same vendor-named child under both --cell-names: the very property
    # `<cell-name>__core` exists to avoid for built-in generators.
    assert children == {cell}


def test_generate_pdk_pcell_cell_name_equal_to_vendor_name_collides(tmp_path):
    """Observed behavior (issue #5933 edge case), pinned rather than endorsed.

    With no ``__core`` suffix, ``--cell-name`` equal to the vendor PCell name
    collides with the child: KLayout uniquifies the *top* to ``<name>$1`` while
    the report's ``cell_name`` still says ``<name>``. Tracked separately; if it
    is fixed (#6128), update this test and the docs clause together.
    """
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "collide.gds"

    report = generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "options": {"cell_name": cell, "output": str(output)},
        }
    )

    assert _read_hierarchy(output) == {
        cell: (False, []),
        f"{cell}$1": (True, [cell]),
    }
    assert report["cell_name"] == cell
