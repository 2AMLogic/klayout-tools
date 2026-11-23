"""Exercise the SG13G2 provisioning script offline, including its compiler probe."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = "fetch-sg13g2-sim-toolchain.sh"


def _executable(path, body):
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (SCRIPT, "_install_common.sh"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    pin = re.search(r'^OPENVAF_R_TAG="(.*?)"$', (scripts / SCRIPT).read_text(), re.M)
    assert pin
    tag = pin[1]

    tech = tmp_path / "pdks/ihp-open-pdk/ihp-sg13g2/libs.tech"
    va = tech / "verilog-a"
    va.mkdir(parents=True)
    osdi = tech / "ngspice/osdi"
    osdi.mkdir(parents=True)
    _executable(
        va / "openvaf-compile-va.sh",
        '#!/bin/sh\necho compile >> "$EVENTS"\ntouch "$MODEL"\n',
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    _executable(
        tools / "ngspice",
        """#!/bin/sh
case "$1" in
  --version) echo 'ngspice-47' ;;
  -b) grep -Fx "pre_osdi $MODEL" "$2" && grep -Fx devhelp "$2" &&
      test -f "$MODEL" && echo verify >> "$EVENTS" ;;
  *) exit 1 ;;
esac
""",
    )
    # The Linux provisioner calls GNU mktemp --suffix. Give its verification
    # deck a fresh .cir path on BSD test hosts as well.
    _executable(
        tools / "mktemp",
        '#!/bin/sh\n[ "$1" = "--suffix=.cir" ] || exit 1\n'
        'dir=$(/usr/bin/mktemp -d "$TMPDIR/verify.XXXXXX") || exit 1\n'
        'touch "$dir/deck.cir" && echo "$dir/deck.cir"\n',
    )
    _executable(tools / "curl", '#!/bin/sh\necho download >> "$EVENTS"\nexit 1\n')
    events = tmp_path / "events"
    events.touch()
    compiler_prefix = tmp_path / "compiler"
    env = {
        "PATH": os.pathsep.join((str(tools), os.defpath)),
        "TMPDIR": str(tmp_path),
        "LC_ALL": "C",
        "OPENVAF_R_INSTALL_PREFIX": str(compiler_prefix),
        "EVENTS": str(events),
        "MODEL": str(osdi / "example.osdi"),
    }

    def run():
        return subprocess.run(
            ["bash", str(scripts / SCRIPT)], env=env, capture_output=True, text=True
        )

    return run, compiler_prefix, osdi, events, tag


def _cache_compiler(prefix, tag, body):
    binary = prefix / "bin/openvaf-r"
    binary.parent.mkdir(parents=True)
    _executable(binary, body)
    (prefix / ".installed-version").write_text(tag + "\n")
    return binary


def test_unstartable_compiler_preserves_loader_error_and_stops(installation):
    run, prefix, osdi, events, tag = installation
    binary = _cache_compiler(prefix, tag, "#!/klt-nonexistent-interpreter-7539\n")

    result = run()

    assert result.returncode != 0
    assert f"cannot start pinned compiler {binary}" in result.stderr
    assert re.search(
        r"bad interpreter|No such file or directory|required file not found",
        result.stderr,
    )
    assert events.read_text() == ""
    assert not (osdi / ".compiled-version").exists()


def test_startable_compiler_is_probed_before_models_are_built(installation):
    run, prefix, osdi, events, tag = installation
    _cache_compiler(
        prefix, tag, '#!/bin/sh\n[ "$1" = --version ] && echo probe >> "$EVENTS"\n'
    )

    result = run()

    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == ["probe", "compile", "verify"]
    assert (osdi / ".compiled-version").read_text().strip() == tag


def test_cached_models_are_verified_without_a_compiler(installation):
    run, prefix, osdi, events, tag = installation
    (osdi / ".compiled-version").write_text(tag + "\n")
    (osdi / "example.osdi").touch()

    result = run()

    assert result.returncode == 0, result.stderr
    assert not prefix.exists()
    assert events.read_text().splitlines() == ["verify"]
