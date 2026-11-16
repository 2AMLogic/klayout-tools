"""Off-host (`batch`/`remote`) `options.osdi_preload` resolution for
`klt characterize` (issue #4911)."""

from __future__ import annotations

import pytest

from klayout_tools import characterize
from test_characterize import _inverter_request


def test_batch_forwards_runner_resolved_osdi_unexpanded(tmp_path, monkeypatch):
    """Issue #4911: on batch a literal `$PDK_ROOT/<rel>` entry skips the local
    existence check and is forwarded unexpanded, in declared order; the same
    entry on `local` still fails `file not found`."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    osdi = tmp_path / "local.osdi"
    osdi.write_text("")
    request = _inverter_request(tmp_path)
    literal = "$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi"
    request["options"] = {"osdi_preload": [literal, "local.osdi"]}
    grid = characterize._resolve_grid(request)
    thresholds = characterize._resolve_thresholds(request)

    batch = characterize._resolve_options(
        request, grid, thresholds, str(tmp_path), backend="batch"
    )
    assert batch["osdi_preload"] == (literal, str(osdi))

    with pytest.raises(
        characterize.CharacterizeError, match="osdi_preload file not found"
    ):
        characterize._resolve_options(
            request, grid, thresholds, str(tmp_path), backend="local"
        )
    with pytest.raises(
        characterize.CharacterizeError, match="osdi_preload file not found"
    ):
        characterize._resolve_options(request, grid, thresholds, str(tmp_path))


def test_batch_models_pdk_root_osdi_is_runner_resolved(tmp_path):
    request = _inverter_request(tmp_path)
    entry = str(tmp_path / "pdk" / "osdi" / "psp103.osdi")
    request["models"] = {"pdk_root": str(tmp_path / "pdk")}
    request["options"] = {"osdi_preload": [entry]}
    grid = characterize._resolve_grid(request)
    thresholds = characterize._resolve_thresholds(request)
    out = characterize._resolve_options(
        request, grid, thresholds, str(tmp_path), backend="batch"
    )
    assert out["osdi_preload"] == (entry,)
