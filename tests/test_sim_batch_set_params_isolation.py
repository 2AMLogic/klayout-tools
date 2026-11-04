"""Issue #4885 item 4: a ``--batch-set`` request must never receive another
request's corners back. ``params_unsupported_worker`` once appeared with a
sibling's corner ('FAST/...' for a TYP-only request); this reproduces the
set path with a fake worker that echoes exactly what it was dispatched and
asserts each request keeps its own corners (negative result: no mix-up in
klt's set/shard path)."""

import json
import threading
import time

from klayout_tools import sim, sim_set
from test_sim_batch import _batch_request, _write_body


def _echo_backend(seen, lock):
    def fake_backend(*, corner_points, **kwargs):
        with lock:
            seen.append([p.corner_id for p in corner_points])
        time.sleep(0.02)  # let sibling requests interleave
        corners = [
            {
                "corner_id": p.corner_id,
                "params": dict(p.params or {}),
                "status": "pass",
                "measurements": [],
                "diagnostics": [],
            }
            for p in corner_points
        ]
        return corners, "46", {"job_id": "job-" + corner_points[0].corner_id}

    return fake_backend


def test_batch_set_requests_keep_their_own_corners(tmp_path, monkeypatch):
    _write_body(tmp_path)
    (tmp_path / "corner.lib").write_text("* lib\n")
    processes = ["TYP", "FAST", "SLOW", "TYP", "FAST", "SLOW"]
    paths = []
    for i, process in enumerate(processes):
        request = _batch_request(
            tmp_path,
            corners={
                "process": [process],
                "params": {"vdd": [1.6, 1.8]},
                "temperature_c": [-40, 27],
            },
        )
        request["models"] = {"lib": "corner.lib"}  # fake backend never reads it
        path = tmp_path / f"req{i}.json"
        path.write_text(json.dumps(request))
        paths.append(str(path))

    seen: list[list[str]] = []
    monkeypatch.setitem(sim._BACKENDS, "batch", _echo_backend(seen, threading.Lock()))

    aggregate = sim_set.run_sim_set(paths, max_in_flight=6)

    assert aggregate["refused_codes"] == [], aggregate["requests"]
    for entry, process in zip(aggregate["requests"], processes, strict=True):
        assert entry["status"] != "error", entry["error"]
        with open(entry["report"], encoding="utf-8") as handle:
            report = json.load(handle)
        ids = [c["corner_id"] for c in report["corners"]]
        assert ids and all(i.startswith(process + "/") for i in ids), ids
