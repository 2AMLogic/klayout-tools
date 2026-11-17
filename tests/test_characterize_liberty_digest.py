"""`klt characterize` pins the emitted `.lib` with `liberty.sha256` (#5835).

The digest is the bare-hex SHA-256 of the file at `liberty.path` — its exact
final on-disk bytes — for the single-cell report and for the one combined
batch library alike; a post-run in-place edit breaks the pin. Split out of
`test_characterize.py` to stay under that file's size-ratchet ceiling.
"""

from __future__ import annotations

from helpers.tool_probes import SKIP_NO_NGSPICE as _SKIP_NO_NGSPICE
from klayout_tools._provenance import sha256_file
from test_characterize import batch_run as batch_run
from test_characterize import example_run as example_run


@_SKIP_NO_NGSPICE
def test_example_liberty_sha256_pins_the_emitted_bytes(example_run):
    """`liberty.sha256` is the bare-hex digest of the file at `liberty.path`,
    and a post-run edit of the `.lib` breaks the pin (#5835)."""
    report, _ = example_run
    lib_path = report["liberty"]["path"]
    digest = report["liberty"]["sha256"]

    assert digest == sha256_file(lib_path)
    assert len(digest) == 64 and not digest.startswith("sha256:")

    with open(lib_path, "ab") as handle:
        handle.write(b"\n")
    assert sha256_file(lib_path) != digest


@_SKIP_NO_NGSPICE
def test_batch_liberty_sha256_pins_the_emitted_bytes(batch_run):
    """The one combined batch `.lib` is pinned the same way (#5835)."""
    report, _ = batch_run

    assert report["liberty"]["sha256"] == sha256_file(report["liberty"]["path"])
