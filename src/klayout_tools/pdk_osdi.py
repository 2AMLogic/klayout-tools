"""Read a resolved PDK's OSDI declarations, verify their digests and probe ngspice."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import env_provenance
from ._digest import sha256_file_required
from .ngspice_build import engine_version, probe_engine_build
from .pdk import find_pdk
from .pdk_families import pdk_variant_family

_DEVICE = re.compile(
    r"^\s*([A-Za-z0-9_.-]+)\s*:\s*A simulator independent device loaded with OSDI\s*$",
    re.I | re.M,
)
_LOADER_ERROR = re.compile(
    r"^\s*(?:error|fatal)\b|couldn.t be loaded|only supports OSDI|"
    r"undefined symbol|failed to load|incompatible OSDI",
    re.I | re.M,
)


class OsdiError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


def _model_path(token: str, asset: Path) -> Path:
    variant = asset.parent.parent
    token = token.replace("${PDK_ROOT}", "$PDK_ROOT").replace("${PDK}", "$PDK")
    for prefix in ("$PDK_ROOT/$PDK/", f"$PDK_ROOT/{variant.name}/"):
        if token.startswith(prefix):
            token = str(variant / token.removeprefix(prefix))
            break
    path = Path(os.path.abspath(asset / token))
    if (
        "$" in token
        or not path.is_relative_to(variant)
        or path.suffix.lower() != ".osdi"
    ):
        raise ValueError("unsupported declaration")
    return path


def declared_models(info: dict[str, Any]) -> list[Path]:
    required = pdk_variant_family(info["variant"]) == "sg13g2"
    asset = info["assets"].get("ngspice")
    setup = Path(asset, ".spiceinit") if asset else None
    if not required and (setup is None or not setup.exists()):
        return []
    try:
        if setup is None:
            raise ValueError("missing ngspice setup")
        result = []
        for line in setup.read_text().splitlines():
            if not re.match(r"^\s*(?:pre_osdi|osdi)(?:\s|$)", line, re.I):
                continue
            _, value = shlex.split(line)
            result.append(_model_path(value, setup.parent))
        if required and not result or len({p.name for p in result}) != len(result):
            raise ValueError("empty required set or duplicate basename")
        return result
    except (OSError, ValueError) as exc:
        raise OsdiError(
            "osdi_setup_unreadable",
            "Cannot establish the declared OSDI model set from the selected PDK setup.",
        ) from exc


def _pins(expect: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        pins = json.loads(Path(expect).read_text())
        if not isinstance(pins, dict) or not all(
            isinstance(digest, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            and "\\" not in name
            and name == Path(name).name
            and name.endswith(".osdi")
            for name, digest in pins.items()
        ):
            raise ValueError("invalid pin")
    except (OSError, ValueError) as exc:
        raise OsdiError(
            "osdi_invalid_expectation",
            "Expected a JSON object mapping model basenames to full SHA-256 digests.",
        ) from exc
    found = {row["name"]: row["sha256"] for row in rows}
    return {
        "missing_from_pins": sorted(found.keys() - pins.keys()),
        "extra_pins": sorted(pins.keys() - found.keys()),
        "digest_mismatches": sorted(
            name
            for name in pins.keys() & found.keys()
            if pins[name].lower() != found[name]
        ),
    }


def _devices(exe: str, directory: Path, load: bool) -> tuple[set[str], str | None]:
    deck = directory / "check.sp"
    preload = "pre_osdi ./model.osdi\n" if load else ""
    deck.write_text(
        f"OSDI registration check\n.control\n{preload}devhelp\nquit\n.endc\n.end\n"
    )
    try:
        result = subprocess.run(
            [exe, "-n", "-b", deck.name],
            cwd=directory,
            env={**os.environ, "HOME": str(directory)},
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set(), "ngspice_unavailable"
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    if result.returncode or _LOADER_ERROR.search(text):
        return set(), "loader_error"
    return set(_DEVICE.findall(text)), None


def _load_one(exe: str, directory: Path, path: Path, expected: str, baseline: set[str]):
    # A fixed name prevents whitespace and ngspice metacharacters in a host
    # path from becoming simulator commands. The link targets the installed file.
    link = directory / "model.osdi"
    try:
        link.symlink_to(path)
        registered, reason = _devices(exe, directory, True)
        if sha256_file_required(str(path)) != expected:
            return set(), "model_changed"
    except OSError:
        return set(), "model_unreadable"
    finally:
        link.unlink(missing_ok=True)
    devices = registered - baseline
    return devices, reason or (None if devices else "no_device_registered")


def _load(paths: list[Path], rows: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = shutil.which("ngspice")
    exe = os.path.realpath(resolved) if resolved else None
    build = probe_engine_build(exe) or {}
    # A raw vendor banner or OS exception can contain an absolute host path.
    version = engine_version(build.get("version_banner") or "")
    digest = build.get("binary_sha256")
    results = []
    with tempfile.TemporaryDirectory(prefix="klt-osdi-") as work:
        area = Path(work)
        baseline, failure = (
            _devices(exe, area, False)
            if exe and version and digest
            else (set(), "ngspice_unavailable")
        )
        for path, model in zip(paths, rows, strict=True):
            devices, reason = (
                _load_one(exe, area, path, model["sha256"], baseline)
                if exe and failure is None
                else (set(), failure)
            )
            results.append(
                {
                    "name": path.name,
                    "loaded": reason is None,
                    "devices": sorted(devices),
                    "reason": reason,
                }
            )
    return {
        "engine_version": version,
        "engine_build": {"binary_sha256": digest},
        "models": results,
    }


def verify_osdi(
    variant: str | None, root: str | None, expect: str | None, load: bool
) -> dict[str, Any]:
    info = find_pdk(variant=variant, root=root)
    paths = declared_models(info)
    repo = env_provenance.repo_root_for(os.getcwd())
    rows = []
    for path in paths:
        present = path.is_file()
        try:
            digest = sha256_file_required(str(path)) if present else None
        except OSError as exc:
            raise OsdiError(
                "osdi_model_unreadable",
                "Cannot read a declared OSDI model.",
                model=path.name,
            ) from exc
        rows.append(
            {
                "name": path.name,
                **env_provenance.repo_relative_path(str(path), repo_root=repo),
                "present": present,
                "sha256": digest,
            }
        )
    identity = {"variant": info["variant"], "models": rows}
    if any(not row["present"] for row in rows):
        raise OsdiError(
            "osdi_model_missing", "A declared OSDI model is missing.", identity=identity
        )
    expectation = _pins(expect, rows) if expect else None
    if expectation and any(expectation.values()):
        raise OsdiError(
            "osdi_pin_mismatch",
            "The declared models differ from the supplied pins.",
            identity=identity,
            expectation=expectation,
        )
    probe = _load(paths, rows) if paths and load else None
    if probe and any(not entry["loaded"] for entry in probe["models"]):
        raise OsdiError(
            "osdi_load_failed",
            "ngspice could not verify every declared OSDI model.",
            identity=identity,
            load_probe=probe,
        )
    return {
        "schema_version": 1,
        "identity": identity,
        "expectation": expectation,
        "load_probe": probe,
    }
