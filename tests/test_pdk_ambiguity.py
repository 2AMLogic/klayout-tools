"""Tests for :mod:`klayout_tools.pdk_ambiguity` (issues #2564, #5449).

Split out of ``test_pdk.py`` (file-size ratchet), alongside the module split.
"""

from __future__ import annotations

from klayout_tools import pdk, pdk_ambiguity

_REPORT = {
    "root": "/home/u/.ciel",
    "variant": "sky130A",
    "resolved_via": "search root: ~/.ciel",
    "ambiguous_roots": [
        {"root": "/home/u/.volare", "resolved_via": "search root: ~/.volare"}
    ],
}

_HEAD = (
    "klt: warning: 2 installs provide PDK variant 'sky130A'; read "
    "/home/u/.ciel (search root: ~/.ciel), skipped /home/u/.volare "
    "(search root: ~/.volare). "
)


def test_ambiguity_warning_is_re_exported_from_pdk():
    """`pdk.ambiguity_warning` / `pdk.AMBIGUITY_REMEDY` are the spellings
    `klt sim`, `klt netlist` and the docs use; the split must not move them
    (issue #5449)."""
    assert pdk.ambiguity_warning is pdk_ambiguity.ambiguity_warning
    assert pdk.AMBIGUITY_REMEDY is pdk_ambiguity.AMBIGUITY_REMEDY


def test_ambiguity_warning_default_remedy_is_unchanged():
    """The #2564 text, byte-for-byte: every caller but `klt sim --backend
    batch` still gets exactly this line."""
    assert pdk.ambiguity_warning(_REPORT) == _HEAD + pdk.AMBIGUITY_REMEDY
    assert "--pdk-root" in pdk.AMBIGUITY_REMEDY
    assert "models.pdk_root" in pdk.AMBIGUITY_REMEDY


def test_ambiguity_warning_uses_the_callers_remedy_when_given():
    """Issue #5449: a caller whose backend refuses the default pins swaps the
    remedy sentence; the hazard half of the line is untouched."""
    alternate = pdk.ambiguity_warning(_REPORT, remedy="Set $PDK_ROOT instead.")

    assert alternate == _HEAD + "Set $PDK_ROOT instead."
    assert "--pdk-root" not in alternate
    assert "models.pdk_root" not in alternate


def test_ambiguity_warning_remedy_none_is_the_default_text():
    """``remedy=None`` is the documented default, not an empty remedy."""
    assert pdk.ambiguity_warning(_REPORT, remedy=None) == pdk.ambiguity_warning(_REPORT)


def test_ambiguity_warning_is_none_when_unambiguous():
    assert pdk.ambiguity_warning({**_REPORT, "ambiguous_roots": []}) is None
    assert pdk.ambiguity_warning({"root": "/a", "variant": "sky130A"}) is None
