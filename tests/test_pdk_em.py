"""Tests for :mod:`klayout_tools.pdk_em` (issues #1215, #1496, #8093).

Split out of ``pdk.py`` (file-size ratchet), alongside the module split. The
behavioural tests for ``em_limits`` / ``resolve_pdk_dbu`` stay in
``test_pdk.py`` (they exercise the ``pdk.`` spellings); what lives here is the
split's own contract: the public re-exports, the ``pdk.find_pdk`` lookup
``em_limits`` performs at call time, and the public tech-LEF enumeration
``pdk_stackup`` shares.
"""

from __future__ import annotations

from klayout_tools import pdk, pdk_em


def test_em_limits_and_resolve_pdk_dbu_are_re_exported_from_pdk():
    """`pdk.em_limits` / `pdk.resolve_pdk_dbu` are the spellings `klt gen`,
    `pdk_output_dbu`, `pdk_pcell`, `gen_ring_layout` and the docs use; the
    split must not move them (issue #8093)."""
    assert pdk.em_limits is pdk_em.em_limits
    assert pdk.resolve_pdk_dbu is pdk_em.resolve_pdk_dbu


def test_em_limits_resolves_find_pdk_through_the_pdk_module(monkeypatch):
    """``em_limits`` reaches ``find_pdk`` as ``pdk.find_pdk`` at call time,
    exactly as it did before the split, so a ``monkeypatch.setattr(pdk,
    "find_pdk", ...)`` in a caller's test stays effective."""
    calls: list[dict] = []

    def _spy(*, variant, root):
        calls.append({"variant": variant, "root": root})
        return {"variant": "sky130A", "root": "/pinned", "assets": {"libs_ref": None}}

    monkeypatch.setattr(pdk, "find_pdk", _spy)

    report = pdk.em_limits(variant="sky130A", root="/pinned")

    assert calls == [{"variant": "sky130A", "root": "/pinned"}]
    assert report == {
        "schema_version": 1,
        "pdk": "sky130A",
        "root": "/pinned",
        "sources": [],
        "layers": [],
        "disagreements": [],
    }


def test_discover_tech_lefs_is_public_and_leaves_no_private_alias_in_pdk():
    """``pdk_stackup`` imports the public ``discover_tech_lefs`` from
    ``pdk_em``; the private ``_discover_tech_lefs`` spelling is not
    re-exported from ``pdk``."""
    assert callable(pdk_em.discover_tech_lefs)
    assert not hasattr(pdk, "_discover_tech_lefs")
    assert pdk_em.discover_tech_lefs("/does-not-exist") == []
