"""Call-history independence of generated streams (issue #5499).

A `klt gen` / `klt layout-plan execute` stream must be a function of
`(request, PDK, klt)` alone -- never of whatever else the calling process
generated earlier, which is not an input anyone can pin or record.

Both tests here drive **subprocesses** and compare **bytes**, for reasons
that are the whole point of the gap they cover:

- The reference PCell library `gen._produce()` uses is registered
  process-globally (`gen._pcell_library()`), so "this request, generated
  first in a fresh process" is only expressible from a fresh interpreter --
  pytest shares one across a session.
- An in-memory `klayout.db` compare (as `tests/test_gen.py`'s
  `_assert_gds_geometry_equal` does) normalises element order on read and
  cannot see a record reorder at all, and neither can `klt diff`'s
  `canonical_hash`.

They live in their own module because `tests/test_gen.py` and
`tests/test_layout_plan_execute.py` are both over the per-file line ratchet's
limit (#2766) and may not grow; the plan-level request builders are imported
from the latter rather than duplicated.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import pdk
from test_layout_plan_execute import (
    _GATE_RAIL_NETLIST,
    _bjt_bus_request,
    _gate_rail_request,
    _make_install,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


# ---------------------------------------------------------------------------
# `klt gen` level
# ---------------------------------------------------------------------------

#: Run `gen.generate()` once per named generator in a single process, then
#: print the sha256 of the **last** stream written. Driven as a subprocess by
#: `test_generate_output_does_not_depend_on_earlier_generators` below.
_GENERATOR_SEQUENCE_SCRIPT = """
import hashlib, os, sys
from klayout_tools import gen, pdk

pdk.STORE_DIRS = []
pdk.CONVENTIONAL_PREFIXES = []
os.environ.pop("PDK_ROOT", None)
os.environ.pop("PDK", None)

pdk_root, out_dir = sys.argv[1], sys.argv[2]
params = {
    "mos_array": {"rows": 1, "cols": 1, "dummy": 0, "gate_contact": True},
    "bjt_array": {"rows": 1, "cols": 8, "dummy": 0, "add_collector_ring": False},
}

for position, name in enumerate(sys.argv[3:]):
    output = os.path.join(out_dir, "%d_%s.gds" % (position, name))
    gen.generate(
        {
            "generator": name,
            "pdk": {"variant": "sky130A", "root": pdk_root},
            "params": params[name],
            "options": {"cell_name": name + "_0", "output": output},
        }
    )

with open(output, "rb") as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
"""


def _generator_sequence_sha256(tmp_path, pdk_root, tag, *sequence):
    """sha256 of the stream `sequence[-1]` writes, generated in a *fresh*
    interpreter that first generated `sequence[:-1]`."""
    import subprocess
    import sys

    script_dir = tmp_path / f"script_{tag}"
    script_dir.mkdir()
    script = script_dir / "sequence.py"
    script.write_text(_GENERATOR_SEQUENCE_SCRIPT)
    out_dir = tmp_path / f"out_{tag}"
    out_dir.mkdir()

    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(pdk_root), str(out_dir), *sequence],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip()


def test_generate_output_does_not_depend_on_earlier_generators(tmp_path, pdk_root):
    """A `generate()` request's stream is a function of that request alone --
    not of which other generators ran earlier in the same process (#5499).

    Before the fix, `_produce()` let the process-global reference PCell
    library's layer table -- which grows in *first-use* order across every
    PCell produced in the process -- reach the written stream: KLayout's
    writers emit each cell's elements in layer-index order, and
    `Layout.add_pcell_variant` mirrored the library's layer order into the
    fresh target layout. So `bjt_array` running first silently reordered the
    element records of the `mos_array` stream that followed it: same
    geometry, same size, different bytes. A flow that commits generated
    streams and gates on a recorded hash then goes red because an *unrelated*
    block was added to the same batch run.
    """
    alone = _generator_sequence_sha256(tmp_path, pdk_root, "alone", "mos_array")
    after_bjt = _generator_sequence_sha256(
        tmp_path, pdk_root, "pair", "bjt_array", "mos_array"
    )
    # A third generator in front, and a repeat of the same one, are equally
    # irrelevant -- the stream depends on the request, nothing else.
    after_two = _generator_sequence_sha256(
        tmp_path, pdk_root, "triple", "bjt_array", "bjt_array", "mos_array"
    )

    assert alone == after_bjt == after_two


# ---------------------------------------------------------------------------
# `klt layout-plan execute` level
# ---------------------------------------------------------------------------

#: Execute each JSON plan document named on argv in one process, in order,
#: then print the sha256 of the **last** one's output stream.
_PLAN_SEQUENCE_SCRIPT = """
import hashlib, json, os, sys
from klayout_tools import pdk
from klayout_tools.layout_plan_execute import execute_layout_plan_document

pdk.STORE_DIRS = []
pdk.CONVENTIONAL_PREFIXES = []
os.environ.pop("PDK_ROOT", None)
os.environ.pop("PDK", None)

request_dir = sys.argv[1]
for path in sys.argv[2:]:
    with open(path, encoding="utf-8") as handle:
        request = json.load(handle)
    execute_layout_plan_document(request, request_dir=request_dir, work_dir=None)

with open(request["options"]["output"], "rb") as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
"""


def _plan_sequence_sha256(tmp_path, tag, requests):
    """sha256 of the stream ``requests[-1]`` writes, executed in a *fresh*
    interpreter that first executed ``requests[:-1]``."""
    import subprocess
    import sys

    script_dir = tmp_path / f"script_{tag}"
    script_dir.mkdir()
    script = script_dir / "sequence.py"
    script.write_text(_PLAN_SEQUENCE_SCRIPT)

    paths = []
    for position, request in enumerate(requests):
        path = tmp_path / f"{tag}_{position}.json"
        path.write_text(json.dumps(request))
        paths.append(str(path))

    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path), *paths],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip()


def _retarget(request, output):
    """``request`` with its output stream redirected to ``output``."""
    return {**request, "options": {**request["options"], "output": str(output)}}


def test_execute_stream_does_not_depend_on_plans_executed_earlier(tmp_path, pdk_root):
    """Executing a `bjt_array` plan and then a `mos_array` plan in one
    process yields, for the second plan, a stream byte-identical to
    executing that plan first in a fresh process (#5499).

    Before the fix the two streams were the same size and geometrically
    identical (zero XOR area on every layer) but byte-different: 18 of the
    49 structures held the same element multiset in a different order. The
    reorder came from `klt gen`'s process-global PCell library, whose layer
    table grows in first-use order and reached the stream twice over --
    once through `Layout.add_pcell_variant` into each generated block, and
    again when `gen_compose` read that block back and KLayout re-linked its
    embedded PCell context info to the live library.

    So a batch run and a single-plan run of the same plan disagreed, and a
    flow gating on a recorded stream hash went red when an unrelated plan
    was added to or removed from the batch -- with nothing in the recorded
    provenance to explain it, since the trigger is not one of the inputs.
    """
    mos_plan = _gate_rail_request(tmp_path, pdk_root, _GATE_RAIL_NETLIST)
    bjt_plan = _bjt_bus_request(
        tmp_path,
        pdk_root,
        {"layer_role": "metal", "width_um": 0.17, "cross_block_layer_role": "metal2"},
    )

    alone = _plan_sequence_sha256(
        tmp_path, "alone", [_retarget(mos_plan, tmp_path / "alone.gds")]
    )
    after_bjt = _plan_sequence_sha256(
        tmp_path,
        "pair",
        [
            _retarget(bjt_plan, tmp_path / "pair_first.gds"),
            _retarget(mos_plan, tmp_path / "pair.gds"),
        ],
    )

    assert alone == after_bjt
