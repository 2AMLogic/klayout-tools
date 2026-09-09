"""Tests for the `eda-sim` overlay image definition under `docker/eda-sim/`
(issue #509).

Nothing here builds a container -- a real build compiles ngspice and xschem
from source and pulls a GHCR base image, which is CI's job
(`.github/workflows/publish-eda-sim-image.yml`), not a unit test's. What these
tests defend is the property that makes the image reviewable at all:

    docker/eda-sim/pdk-versions.json is the SINGLE SOURCE OF TRUTH for every
    version the image bakes or fetches.

`build.sh` reads that manifest and passes each pin to `docker build` as a
`--build-arg`; the Dockerfile declares those ARGs *without defaults*. So a
version literal appearing anywhere else -- a Dockerfile default, a hardcoded
tag in the workflow -- is drift waiting to happen: the manifest would say one
thing and the built image would contain another, and nothing would fail. The
"no duplicated version literals" tests below are that guard, and they are the
structural half of the fix rather than a comment asking future editors to be
careful.

The rest assert the issue's own hard rules are still mechanically true: the
PDK tree is never baked, `PDK_ROOT` is never set, and `magic`/`netgen` are not
installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
EDA_SIM_DIR = REPO_ROOT / "docker" / "eda-sim"
MANIFEST_PATH = EDA_SIM_DIR / "pdk-versions.json"
DOCKERFILE = EDA_SIM_DIR / "Dockerfile"
BUILD_SH = EDA_SIM_DIR / "build.sh"
SMOKE_SH = EDA_SIM_DIR / "smoke.sh"
FETCH_PDK = EDA_SIM_DIR / "eda-sim-fetch-pdk"
README = EDA_SIM_DIR / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-eda-sim-image.yml"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


# --------------------------------------------------------------------------
# The manifest itself
# --------------------------------------------------------------------------


def test_every_file_exists_and_scripts_are_executable():
    for path in (MANIFEST_PATH, DOCKERFILE, README, WORKFLOW):
        assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    for script in (BUILD_SH, SMOKE_SH, FETCH_PDK):
        assert script.is_file(), f"missing {script.relative_to(REPO_ROOT)}"
        assert script.stat().st_mode & 0o111, (
            f"{script.relative_to(REPO_ROOT)} is not executable; "
            "the Dockerfile COPYs it onto PATH and CI invokes it directly"
        )


def test_manifest_has_every_pin_the_build_needs(manifest):
    assert manifest["schema_version"] == 1

    assert manifest["image"]["name"].startswith("ghcr.io/")
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["image"]["version"])
    assert isinstance(manifest["image"]["max_size_bytes"], int)

    assert manifest["base"]["image"] == "ghcr.io/rjwalters/loom-worker"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["base"]["version"])

    ngspice = manifest["tools"]["ngspice"]
    assert re.fullmatch(r"[0-9a-f]{64}", ngspice["sha256"]), (
        "ngspice's tarball digest must be a full sha256 -- it is the only thing "
        "standing between the image and a substituted download"
    )
    assert int(ngspice["version"]) >= ngspice["min_major"], (
        "the pinned ngspice version must itself clear the floor the image asserts"
    )
    assert "{version}" in ngspice["url_template"]

    xschem = manifest["tools"]["xschem"]
    assert re.fullmatch(r"[0-9a-f]{40}", xschem["commit"])
    assert xschem["repo"].startswith("https://")

    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", manifest["tools"]["ciel"]["version"])
    assert re.fullmatch(
        r"\d+\.\d+(\.\d+)?", manifest["tools"]["klayout_tools"]["version"]
    )


def test_pdk_tree_is_declared_not_baked(manifest):
    """Issue #509's hard rule, asserted against the manifest that drives the build."""
    assert manifest["pdks"]["baked"] is False
    for family, entry in manifest["pdks"]["families"].items():
        assert re.fullmatch(r"[0-9a-f]{40}", entry["open_pdks_commit"]), (
            f"{family}: open_pdks must be pinned to a full commit sha, "
            "never a floating tag -- the hash IS the device models"
        )
        assert entry["libraries"], f"{family}: needs at least the primitive library"
        assert entry["variant"].startswith(family[:6])


def test_manifest_pins_both_pdk_families(manifest):
    assert set(manifest["pdks"]["families"]) == {"sky130", "gf180mcu"}


# --------------------------------------------------------------------------
# Single source of truth: no version literal is duplicated outside the manifest
# --------------------------------------------------------------------------

#: Pins whose literal value must NOT appear in the Dockerfile or the workflow.
#: Deliberately excludes `image.name`/`base.image` (repository *paths*, not
#: versions -- they name the artifacts and are safe to read from the manifest
#: at runtime) and `image.version` (the workflow reads it via jq into a job
#: output, which is the sanctioned path).
_VERSION_PIN_PATHS = [
    ("base", "version"),
    ("tools", "ngspice", "version"),
    ("tools", "ngspice", "sha256"),
    ("tools", "xschem", "tag"),
    ("tools", "xschem", "commit"),
    ("tools", "ciel", "version"),
    ("tools", "klayout_tools", "version"),
]


def _pin(manifest: dict, path: tuple[str, ...]):
    node = manifest
    for key in path:
        node = node[key]
    return str(node)


def _code_only(path: Path) -> str:
    """The file with `#` comments removed.

    Prose is allowed to *name* a pin -- the Dockerfile's comments explain why
    ngspice is built from source and cite the `46` floor, and the README quotes
    every value. What must not happen is a pin becoming *executable* outside
    the manifest, so the duplication checks below look at code only.
    """
    out = []
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


@pytest.mark.parametrize("pin_path", _VERSION_PIN_PATHS, ids=lambda p: ".".join(p))
def test_no_version_literal_is_duplicated_in_the_dockerfile(manifest, pin_path):
    """A Dockerfile ARG default would silently win over the manifest.

    The whole no-drift design rests on the Dockerfile having no version
    literals at all: `build.sh` supplies every pin, and each guarded step uses
    `${VAR:?...}` so a missing one fails the build loudly. If someone adds
    `ARG NGSPICE_VERSION=46` as a convenience default, the manifest stops
    being authoritative the moment the two disagree -- and nothing else in the
    repo would notice.
    """
    value = _pin(manifest, pin_path)
    body = _code_only(DOCKERFILE)
    assert value not in body, (
        f"{'.'.join(pin_path)} = {value!r} is hardcoded in docker/eda-sim/Dockerfile. "
        "Pins live only in pdk-versions.json; build.sh passes them as --build-arg."
    )


@pytest.mark.parametrize("pin_path", _VERSION_PIN_PATHS, ids=lambda p: ".".join(p))
def test_no_version_literal_is_duplicated_in_the_workflow(manifest, pin_path):
    value = _pin(manifest, pin_path)
    body = _code_only(WORKFLOW)
    assert value not in body, (
        f"{'.'.join(pin_path)} = {value!r} is hardcoded in "
        ".github/workflows/publish-eda-sim-image.yml. The workflow must read "
        "pins through `docker/eda-sim/build.sh`, never restate them."
    )


def test_dockerfile_declares_every_build_arg_build_sh_passes():
    """`build.sh` and the Dockerfile must agree on the ARG names.

    A `--build-arg` for an ARG the Dockerfile never declares is silently
    ignored by Docker (it only warns), so a rename on one side would drop a
    pin without failing anything.
    """
    passed = {
        line.split("=", 1)[0]
        for line in subprocess.run(
            [str(BUILD_SH), "--print-args"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if line.strip()
    }
    declared = set(re.findall(r"^ARG\s+([A-Z0-9_]+)", DOCKERFILE.read_text(), re.M))
    missing = passed - declared
    assert not missing, (
        f"build.sh passes --build-arg for {sorted(missing)}, which the Dockerfile "
        "never declares -- Docker ignores those, so the pin would be silently dropped"
    )


def test_build_sh_print_args_resolves_the_manifest_pins(manifest):
    args = dict(
        line.split("=", 1)
        for line in subprocess.run(
            [str(BUILD_SH), "--print-args"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if line.strip()
    )
    assert args["LOOM_WORKER_VERSION"] == manifest["base"]["version"]
    assert args["NGSPICE_VERSION"] == manifest["tools"]["ngspice"]["version"]
    assert args["NGSPICE_SHA256"] == manifest["tools"]["ngspice"]["sha256"]
    assert args["XSCHEM_COMMIT"] == manifest["tools"]["xschem"]["commit"]
    assert args["CIEL_VERSION"] == manifest["tools"]["ciel"]["version"]
    assert (
        args["KLAYOUT_TOOLS_VERSION"] == manifest["tools"]["klayout_tools"]["version"]
    )
    # The URL template must have been expanded, not passed through raw.
    assert "{version}" not in args["NGSPICE_URL"]
    assert manifest["tools"]["ngspice"]["version"] in args["NGSPICE_URL"]


def test_dockerfile_args_have_no_defaults():
    """Every pin ARG must be `ARG NAME`, never `ARG NAME=value`.

    `DEBIAN_FRONTEND` is the one allowed exception: it is a build-time apt
    setting, not a version pin.
    """
    defaulted = re.findall(r"^ARG\s+([A-Z0-9_]+)=", DOCKERFILE.read_text(), re.M)
    assert set(defaulted) <= {"DEBIAN_FRONTEND"}, (
        f"Dockerfile ARGs with defaults: {sorted(set(defaulted))}. A default lets a "
        "build succeed with a stale pin instead of failing loudly."
    )


# --------------------------------------------------------------------------
# The hard rules, asserted against the Dockerfile
# --------------------------------------------------------------------------


def test_dockerfile_never_sets_pdk_root():
    """ciel installs into `$PDK_ROOT` when it is set, and into `~/.ciel/<variant>`
    when it is not. Every audited fleet harness searches `~/.ciel`, so setting
    `PDK_ROOT` here would redirect installs somewhere they will not look.
    """
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.match(r"ENV\s+PDK_ROOT", stripped), (
            "the image must not set PDK_ROOT -- see docker/eda-sim/README.md"
        )


def test_dockerfile_does_not_bake_a_pdk():
    body = DOCKERFILE.read_text()
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    for forbidden in ("ciel enable", "volare enable", "volare fetch", "ciel fetch"):
        assert forbidden not in code, (
            f"'{forbidden}' in the Dockerfile would bake the PDK tree into the "
            "image, which issue #509 forbids (7-9 GB per family). The fetch "
            "happens at runtime via eda-sim-fetch-pdk."
        )


def test_dockerfile_installs_neither_magic_nor_netgen():
    """Audited 2026-08-04, spot-checked 2026-09-09: no gf180-*/sky130-* block
    repo's CI invokes magic or netgen, and this repo's own LVS engine
    (`src/klayout_tools/lvs.py`) is netlist-vs-netlist with no magic backend.
    """
    code = "\n".join(
        line
        for line in DOCKERFILE.read_text().splitlines()
        if not line.strip().startswith("#")
    )
    for tool in ("magic", "netgen"):
        assert not re.search(rf"^\s+{tool}\s*\\?$", code, re.M), (
            f"{tool} is installed by the Dockerfile but nothing in the audited "
            "fleet CI uses it -- see docker/eda-sim/README.md"
        )


def test_dockerfile_uses_the_pinned_base_image_arg():
    body = DOCKERFILE.read_text()
    from_lines = [
        line for line in body.splitlines() if line.strip().startswith("FROM ")
    ]
    assert from_lines, "no FROM instruction"
    for line in from_lines:
        assert "${LOOM_WORKER_IMAGE}:${LOOM_WORKER_VERSION}" in line, (
            f"FROM must resolve the base through the manifest-supplied ARGs: {line}"
        )


def test_dockerfile_verifies_the_ngspice_digest_before_unpacking():
    body = DOCKERFILE.read_text()
    check_at = body.find("sha256sum --check --strict")
    untar_at = body.find("tar xzf")
    assert check_at != -1, "the ngspice tarball's digest is never checked"
    assert untar_at != -1
    assert check_at < untar_at, (
        "the sha256 check must run BEFORE the tarball is unpacked"
    )


def test_dockerfile_asserts_the_xschem_tag_resolves_to_the_pinned_commit():
    body = DOCKERFILE.read_text()
    assert "XSCHEM_COMMIT" in body and "git rev-parse HEAD" in body, (
        "the Dockerfile must assert the cloned xschem tag resolves to the pinned "
        "commit -- an upstream tag can be re-pointed"
    )


# --------------------------------------------------------------------------
# Workflow wiring
# --------------------------------------------------------------------------


def test_workflow_builds_through_build_sh_not_a_bare_docker_build():
    body = WORKFLOW.read_text()
    assert "docker/eda-sim/build.sh" in body
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.match(r"run:\s*docker (buildx )?build\b", stripped), (
            "the workflow must build via docker/eda-sim/build.sh so pins come "
            f"from the manifest: {stripped}"
        )


def test_workflow_asserts_the_size_ceiling():
    body = WORKFLOW.read_text()
    assert "docker image inspect --format '{{.Size}}'" in body
    assert "max_size_bytes" in body, (
        "the size ceiling must be read from the manifest, not restated in YAML"
    )


def test_workflow_runs_the_baked_smoke_script():
    body = WORKFLOW.read_text()
    assert "/opt/eda/smoke.sh" in body, (
        "CI must run the same smoke script that is baked into the image, so a "
        "fleet node can re-verify exactly what CI verified"
    )
    assert "--with-pdk" in body, (
        "the PDK-backed stage is the acceptance test for the runtime-fetch rule"
    )


def test_workflow_builds_both_architectures():
    body = WORKFLOW.read_text()
    assert "linux/amd64" in body and "linux/arm64" in body, (
        "the loom-worker base is a multi-arch manifest; this overlay must match "
        "it or arm64 fleet nodes cannot run the image"
    )


# --------------------------------------------------------------------------
# Shell hygiene
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script", [BUILD_SH, SMOKE_SH, FETCH_PDK], ids=lambda p: p.name
)
def test_shell_scripts_pass_shellcheck(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    proc = subprocess.run(
        ["shellcheck", "--severity=warning", str(script)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"shellcheck findings:\n{proc.stdout}{proc.stderr}"


@pytest.mark.parametrize(
    "script", [BUILD_SH, SMOKE_SH, FETCH_PDK], ids=lambda p: p.name
)
def test_shell_scripts_are_syntactically_valid(script):
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
