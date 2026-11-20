"""``klt gen``'s hidden-parameter contract (issue #6757).

Internal PCell declarations are marked ``hidden=True`` where their
generators declare them, and the retired ``gen._HIDDEN_PARAMS`` registry's
two guarantees must hold unchanged: ``klt gen --list`` does not advertise
them, and the request validator rejects them from caller input.
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.gen import GenError, generate


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    ("generator", "name"),
    [
        # ``res_array``'s generated mask slot (``res_flavor_<i>_*``).
        ("res_array", "res_flavor_0_layer"),
        # A shared plumbing declaration from ``gen_pcells/_common_params.py``.
        ("res_array", "contact_layer"),
        ("mos_array", "voltage_flavor_ring_margin_um"),
        # The per-family geometry floor that leaked into ``--list`` via #1455.
        ("cap_array", "cap_top_via_metal_min_w_um"),
    ],
)
def test_hidden_declaration_rejected_as_unknown_param(
    tmp_path, pdk_root, generator, name
):
    """A request naming a hidden declaration fails exactly as it did under
    the retired global registry."""
    with pytest.raises(GenError, match="unknown params"):
        generate(
            {
                "generator": generator,
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {name: 1},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
