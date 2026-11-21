"""``models.pdk_root`` pin expansion for ``options.osdi_preload`` (issue #5227)."""

from __future__ import annotations

import pytest

from klayout_tools.sim_errors import SimError
from klayout_tools.sim_osdi_pin import resolve_osdi_preload


@pytest.mark.parametrize("prefix", ["$PDK_ROOT", "${PDK_ROOT}"])
def test_resolve_osdi_preload_expands_pdk_root_pin_with_env_unset(
    tmp_path, monkeypatch, prefix
):
    # Issue #5227: a local run honours the pin for a literal `$PDK_ROOT/...`.
    monkeypatch.delenv("PDK_ROOT", raising=False)
    pin = tmp_path / "pdk"
    (pin / "lib").mkdir(parents=True)
    (pin / "lib" / "psp103.osdi").write_bytes(b"\x00")
    options = {"osdi_preload": [f"{prefix}/lib/psp103.osdi"]}
    assert resolve_osdi_preload(options, str(tmp_path), pdk_root=str(pin)) == (
        str(pin / "lib" / "psp103.osdi"),
    )


def test_resolve_osdi_preload_without_pin_or_env_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    options = {"osdi_preload": ["$PDK_ROOT/lib/psp103.osdi"]}
    with pytest.raises(SimError, match="osdi_preload file not found: .*PDK_ROOT"):
        resolve_osdi_preload(options, str(tmp_path))


def test_resolve_osdi_preload_env_used_without_pin_and_pin_wins_over_env(
    tmp_path, monkeypatch
):
    env_root = tmp_path / "env"
    pin_root = tmp_path / "pin"
    for root in (env_root, pin_root):
        root.mkdir()
        (root / "m.osdi").write_bytes(b"\x00")
    monkeypatch.setenv("PDK_ROOT", str(env_root))
    options = {"osdi_preload": ["$PDK_ROOT/m.osdi", "plain.osdi"]}
    (tmp_path / "plain.osdi").write_bytes(b"\x00")
    assert resolve_osdi_preload(options, str(tmp_path))[0] == str(env_root / "m.osdi")
    resolved = resolve_osdi_preload(options, str(tmp_path), pdk_root=str(pin_root))
    # Pin wins; an entry without the prefix is untouched.
    assert resolved == (str(pin_root / "m.osdi"), str(tmp_path / "plain.osdi"))
