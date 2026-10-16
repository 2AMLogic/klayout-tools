"""open_pdks pin checks for sky130A / gf180mcuD (issue #3427, epic #3420).

Hermetic: fixture trees under ``tmp_path`` and a ``STORE_DIRS`` override.
"""

import json
from pathlib import Path

import pytest

from klayout_tools import _provenance, pdk, pdk_compat

PIN = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
OLD = "c6d73a35f524070e85faff4a6a9eef49553ebc2b"
MANIFEST = Path(__file__).resolve().parent.parent / "docker/eda-sim/pdk-versions.json"


def _install(root, variant="sky130A", sources=None):
    variant_dir = root / variant
    (variant_dir / "libs.tech" / "ngspice").mkdir(parents=True)
    if sources is not None:
        (variant_dir / "SOURCES").write_text(sources, encoding="utf-8")
    return variant_dir


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_pin_mirrors_the_eda_sim_manifest():
    families = json.loads(MANIFEST.read_text())["pdks"]["families"]
    assert {f["open_pdks_commit"] for f in families.values()} == {
        pdk_compat.OPEN_PDKS_PIN
    }
    assert pdk_compat.OPEN_PDKS_PIN == PIN


@pytest.mark.parametrize("variant", ["sky130A", "gf180mcuD"])
@pytest.mark.parametrize(
    ("stamp", "status"),
    [
        (f"open_pdks {PIN}\n", "match"),
        (f"open_pdks {PIN[:9]}\n", "match"),
        (f"magic 8.3\nopen_pdks {PIN}\n", "match"),
        (f"open_pdks {OLD}\n", "mismatch"),
        (f"open_pdks {PIN[:5]}\n", "mismatch"),
        (None, "unknown"),
    ],
)
def test_status_for_stamp(tmp_path, variant, stamp, status):
    root = tmp_path / "root"
    _install(root, variant, stamp)
    compat = pdk.find_pdk(variant=variant, root=str(root))["compatibility"]
    assert compat["status"] == status
    assert compat["expected"] == {
        "namespace": "open_pdks-sources",
        "value": f"open_pdks {PIN}",
    }


def test_ciel_preferred_over_volare_and_volare_is_ambiguous(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install(home / ".ciel", sources=f"open_pdks {PIN}\n")
    _install(home / ".volare", sources=f"open_pdks {OLD}\n")
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel", "~/.volare"])
    report = pdk.find_pdk(variant="sky130A")
    assert report["compatibility"]["status"] == "match"
    assert [a["resolved_via"] for a in report["ambiguous_roots"]] == [
        "search root: ~/.volare"
    ]


def test_volare_only_old_tree_reports_mismatch_in_provenance(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install(home / ".volare", sources=f"open_pdks {OLD}\n")
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel", "~/.volare"])
    report = pdk.find_pdk(variant="sky130A")
    assert report["compatibility"]["status"] == "mismatch"
    block = _provenance._pdk_block(report)
    assert block["compatibility"] == {
        "status": "mismatch",
        "package": "open_pdks-sky130",
        "expected": f"open_pdks {PIN}",
    }


def test_provenance_omits_compatibility_when_matching(tmp_path):
    root = tmp_path / "root"
    _install(root, sources=f"open_pdks {PIN}\n")
    block = _provenance._pdk_block(pdk.find_pdk(variant="sky130A", root=str(root)))
    assert "compatibility" not in block
