"""IHP_PDKS_DIR / IHP_OPEN_PDK_PIN_FILE in scripts/fetch-ihp-sg13g2.sh (issue #3809).

Offline only: the cached-marker path is exercised for the happy cases, and
every pin-file rejection must exit non-zero before any download starts.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch-ihp-sg13g2.sh"
SHA = "ab" * 32


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    # PATH lacks nothing network-wise, but a bad pin must exit before curl.
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


def _seed(tmp_path: Path, version: str) -> Path:
    dest = tmp_path / "pdks" / "ihp-open-pdk"
    dest.mkdir(parents=True)
    (dest / ".fetched-version").write_text(version + "\n")
    return dest


def _pin(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "pin.env"
    p.write_bytes(text.encode())
    return p


def test_pdks_dir_override_honored(tmp_path):
    dest = _seed(tmp_path, "0.3.0")
    r = _run(tmp_path, IHP_PDKS_DIR=str(dest.parent))
    assert r.returncode == 0
    assert f"already present at {dest}" in r.stdout


def test_pin_file_version_drives_cache_check(tmp_path):
    dest = _seed(tmp_path, "9.9.9")
    pin = _pin(
        tmp_path,
        "# consumer pin\r\n\r\nUNKNOWN=1\r\n"
        f"IHP_OPEN_PDK_VERSION=9.9.9\r\nIHP_OPEN_PDK_SHA256={SHA}\r\n",
    )
    r = _run(tmp_path, IHP_PDKS_DIR=str(dest.parent), IHP_OPEN_PDK_PIN_FILE=str(pin))
    assert r.returncode == 0
    assert "v9.9.9 already present" in r.stdout


def test_pin_file_is_not_sourced(tmp_path):
    dest = _seed(tmp_path, "9.9.9")
    marker = tmp_path / "pwned"
    pin = _pin(
        tmp_path,
        f"touch {marker}\nIHP_OPEN_PDK_VERSION=9.9.9\nIHP_OPEN_PDK_SHA256={SHA}\n",
    )
    r = _run(tmp_path, IHP_PDKS_DIR=str(dest.parent), IHP_OPEN_PDK_PIN_FILE=str(pin))
    assert r.returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "text",
    [
        f"IHP_OPEN_PDK_SHA256={SHA}\n",  # missing version
        "IHP_OPEN_PDK_VERSION=1.2.3\n",  # missing sha
        f"IHP_OPEN_PDK_VERSION=v1.2\nIHP_OPEN_PDK_SHA256={SHA}\n",  # bad version
        "IHP_OPEN_PDK_VERSION=1.2.3\nIHP_OPEN_PDK_SHA256=abc123\n",  # bad sha
        f"IHP_OPEN_PDK_VERSION=1.2.3\nIHP_OPEN_PDK_VERSION=1.2.4\nIHP_OPEN_PDK_SHA256={SHA}\n",
        f"IHP_OPEN_PDK_VERSION=1.2.3\nIHP_OPEN_PDK_SHA256={SHA}\nIHP_OPEN_PDK_SHA256={SHA}\n",
    ],
)
def test_bad_pin_file_fails_before_download(tmp_path, text):
    pin = _pin(tmp_path, text)
    r = _run(
        tmp_path, IHP_PDKS_DIR=str(tmp_path / "pdks"), IHP_OPEN_PDK_PIN_FILE=str(pin)
    )
    assert r.returncode != 0
    assert "error:" in r.stderr
    assert "Downloading" not in r.stdout


def test_nonexistent_pin_file_fails(tmp_path):
    r = _run(
        tmp_path,
        IHP_PDKS_DIR=str(tmp_path / "pdks"),
        IHP_OPEN_PDK_PIN_FILE=str(tmp_path / "nope"),
    )
    assert r.returncode != 0
    assert "not a readable file" in r.stderr
    assert "Downloading" not in r.stdout
