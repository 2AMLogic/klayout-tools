"""The extraction leaf and its consumers can run without loading the orchestrator."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("netlist_digest", ""),
        ("lvs_gate_level", ""),
        ("netlist_capacitor_recovery", "m._mom_feed_code(None)"),
        ("extract_batch", "m.argv_request({'file': '/tmp/a.gds', 'deck': 'sky130'})"),
        ("extract_report", "m._describe_devices(kdb.Circuit())"),
        (
            "extract_detect_markers",
            "m._detect_unmodelled_poly_bodies(*[kdb.Region() for _ in range(4)])",
        ),
        (
            "extract_pdk_resolve",
            "m.resolve_pdk_and_model_bindings(deck_name='sky130', deck_options=None, "
            "pdk_variant=None, pdk_root=None, device_models=None)",
        ),
    ],
)
def test_consumer_runs_without_extract(module, call):
    script = (
        "import sys\n"
        "import klayout.db as kdb\n"
        f"import klayout_tools.{module} as m\n"
        f"{call}\n"
        "assert 'klayout_tools.extract' not in sys.modules"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_leaf_loads_only_standard_library_dependencies():
    script = (
        "import sys, klayout_tools.extract_base\n"
        "assert 'klayout' not in sys.modules\n"
        "assert [m for m in sys.modules if m.startswith('klayout_tools.')] "
        "== ['klayout_tools.extract_base']"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_public_imports_share_implementation_objects():
    from klayout_tools import (
        extract,
        extract_base,
        extract_parasitics,
        extract_passive_regions,
        extract_spef,
    )

    assert (
        extract_spef.ExtractError is extract.ExtractError is extract_base.ExtractError
    )
    assert (
        extract_parasitics.spice_safe_net_name
        is extract.spice_safe_net_name
        is extract_base.spice_safe_net_name
    )
    assert (
        extract_passive_regions.MOM_FEED_TOKENS
        is extract.MOM_FEED_TOKENS
        is extract_base.MOM_FEED_TOKENS
    )
    for name in (
        "SCHEMA_VERSION",
        "REQUEST_SCHEMA",
        "PARASITIC_MODEL_SCOPE",
        "SUBSTRATE_DC_TIE_RESISTANCE_OHM",
    ):
        assert getattr(extract, name) is getattr(extract_base, name)
