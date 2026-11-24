"""Translate the default sim fleet's published eda-batch-image/1 record."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .pdk_families import pdk_variant_family


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _open_pdks_commit(inventory: dict[str, Any], family: str) -> str | None:
    pin = _text(inventory.get(f"{family}_open_pdks"))
    return pin if pin and re.fullmatch(r"[0-9a-f]{40}", pin) else None


def _identity_rows(
    record: Any, pdk: str | None, region: str | None
) -> list[dict[str, Any]]:
    if not isinstance(record, dict) or record.get("schema") != "eda-batch-image/1":
        return []
    ami, location = _text(record.get("ami_id")), _text(record.get("region"))
    if not ami or not re.fullmatch(r"ami-(?:[a-f0-9]{8}|[a-f0-9]{17})", ami):
        return []
    if not location or (region and region != location):
        return []
    tools, inventory = record.get("tools"), record.get("pdk")
    if not isinstance(tools, dict) or not isinstance(inventory, dict):
        return []
    variants = _text(inventory.get("variants"))
    if not variants:
        return []
    installed = variants.split()
    families = {pdk_variant_family(v) for v in ([pdk] if pdk else installed)}
    version = _text(tools.get("klt_build")) or _text(tools.get("klt"))
    engine = _text(tools.get("ngspice"))
    return [
        {
            "pdk": "sky130A" if family == "sky130" else family,
            "region": location,
            "ami_id": ami,
            "klt_version": version,
            "klt_ref": None,  # source_commit identifies the 2am bake, not klt.
            "pdk_variants": installed,
            "open_pdks_commit": _open_pdks_commit(inventory, family),
            "ngspice_version": engine.removeprefix("ngspice-") if engine else None,
            "pdk_snapshot": None,
        }
        for family in sorted(families)
    ]


def read_batch_runner_images(
    pdk: str | None,
    region: str | None,
    get_object: list[str],
    run: Callable[[list[str], float], subprocess.CompletedProcess[str]],
) -> list[dict[str, Any]]:
    """Read one small S3 object. An unavailable or invalid pointer is unknown."""
    # The provisioner routes this variant to a separate image profile.
    if pdk and pdk.startswith("ihp-sg13cmos5l"):
        return []
    try:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "current.json"
            result = run([*get_object, str(path)], 5.0)
            if result.returncode:
                return []
            return _identity_rows(json.loads(path.read_bytes()), pdk, region)
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        return []
