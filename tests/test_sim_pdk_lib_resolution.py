"""PDK model-library resolution on flat single-PDK installs (issue #6256).

Regression tests moved out of ``tests/test_sim.py`` when that file reached
its trusted size ceiling (``file-size-baseline.json``): a flat install
(issue #522 shape) is one where ``--pdk-root`` names the PDK's own
directory, so the resolved variant directory *is* the root itself and the
naive ``root/<variant>`` join produces a phantom path. These tests pin the
layout-aware rule (``klayout_tools.pdk.variant_dir_of``) for
``models.lib``, ``_resolve_lib_ref``, and per-corner-section ``lib`` refs.
"""

from __future__ import annotations

from klayout_tools import sim


def test_resolve_models_lib_via_flat_pdk_install(tmp_path):
    """Flat single-PDK install (issue #522 shape): ``--pdk-root`` names the
    PDK's own directory, so the variant directory is the root itself --
    ``root/<variant>`` is a phantom path here (issue #6256)."""
    install_root = tmp_path / "sg13g2"
    lib_dir = install_root / "libs.tech" / "ngspice"
    lib_dir.mkdir(parents=True)
    lib_path = lib_dir / "sg13g2.lib.spice"
    lib_path.write_text(".lib tt\n.endl tt\n")

    resolved = sim._resolve_models_lib(
        {
            "pdk": "sg13g2",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sg13g2.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert resolved == str(lib_path)


def test_resolve_lib_ref_flat_install_joins_against_root(tmp_path):
    """The ``_resolve_lib_ref`` rule itself on a flat install: a relative ref
    joins against the root -- the variant directory -- not the phantom
    ``root/<variant>`` (issue #6256)."""
    install_root = tmp_path / "sg13g2"
    (install_root / "libs.tech").mkdir(parents=True)

    resolved = sim._resolve_lib_ref(
        "models/x.lib",
        {"pdk": "sg13g2", "pdk_root": str(install_root)},
        request_dir=str(tmp_path),
    )

    assert resolved == str(install_root / "models" / "x.lib")


def test_resolve_corner_section_libs_joins_relative_refs_against_flat_pdk_variant(
    tmp_path,
):
    """Flat single-PDK install (issue #522 shape): the variant directory is
    the root itself, so a per-section ``lib`` ref resolves against it -- not
    the phantom ``root/<variant>`` (issue #6256)."""
    install_root = tmp_path / "sg13g2"
    lib_dir = install_root / "libs.tech" / "ngspice"
    lib_dir.mkdir(parents=True)
    cap = lib_dir / "cap.lib"
    cap.write_text(".lib cap_tt\n.endl cap_tt\n")

    (point,) = sim._expand_corners(
        {
            "process": [
                {
                    "name": "tt",
                    "sections": [
                        {
                            "lib": "libs.tech/ngspice/cap.lib",
                            "section": "cap_tt",
                        }
                    ],
                }
            ]
        },
        [],
    )
    libs = sim._resolve_corner_section_libs(
        [point],
        {"pdk": "sg13g2", "pdk_root": str(install_root)},
        str(tmp_path),
    )

    assert libs == (str(cap),)
