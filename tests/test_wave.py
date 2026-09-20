"""Tests for `klt wave build`/`klt wave query` (Epic #1585 Phase 2c, issue
#1601): the Python wiring (`klayout_tools/wave.py`) that turns `native/wave`'s
standalone `klt-wave` binary (issues #1599/#1600) into `klt wave` CLI
subcommands, plus schema-self-consistency checks for
`docs/schemas/wave-*.schema.json`.

Three tiers, mirroring `test_techmap_equiv_gate.py`'s own structure:

- **Schema self-consistency** (no binary needed): each of the four schema
  files is well-formed JSON Schema, and a real captured request/response
  document validates against its own schema.
- **Wrapper unit tests** (no binary needed): `wave.py`'s own
  `_binary_path()`-equivalent degrade-cleanly error path when the native
  binary has not been built, mirroring `techmap.py::_binary_path()`'s
  documented convention.
- **End-to-end integration test** (real binary, skipped without `cargo`):
  builds the real `klt-wave` binary on demand, runs `klt wave build`
  against the real canary fixture `native/wave/tests/fixtures/
  modexp_canary.vcd` (the same fixture `native/wave`'s own
  `tests/modexp_canary_test.rs` uses, generated from
  `examples/functional-verification/modexp.v`'s real Icarus-simulated
  trace -- see that fixture's own `tests/fixtures/README.md` regeneration
  recipe), then `klt wave query`s the resulting store and asserts a known
  handshake count (`tb_modexp.done` rises exactly 3 times, one per
  `run_case` vector) -- the concrete "trace a real canary testbench,
  build+query it, assert a known handshake count" scenario both the epic's
  own Test Plan and this issue's Acceptance Criteria name.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

from klayout_tools import wave
from klayout_tools.wave import WaveError, run_wave_build, run_wave_query

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "docs" / "schemas"
MODEXP_FIXTURE = (
    REPO_ROOT / "native" / "wave" / "tests" / "fixtures" / "modexp_canary.vcd"
)

HAVE_CARGO = shutil.which("cargo") is not None

# ---------------------------------------------------------------------------
# Schema self-consistency (docs/schemas/wave-*.schema.json)
# ---------------------------------------------------------------------------

SCHEMA_FILES = [
    "wave-build-request.schema.json",
    "wave-build-response.schema.json",
    "wave-query-request.schema.json",
    "wave-query-response.schema.json",
]


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text())


@pytest.mark.parametrize("schema_name", SCHEMA_FILES)
def test_schema_file_is_valid_json(schema_name):
    schema = _load_schema(schema_name)
    assert isinstance(schema, dict)


@pytest.mark.parametrize("schema_name", SCHEMA_FILES)
def test_schema_is_well_formed_json_schema(schema_name):
    schema = _load_schema(schema_name)
    jsonschema.Draft202012Validator.check_schema(schema)


_BUILD_REQUEST_DOC = {
    "schema": "klt.wave_build.request/1",
    "trace": {"path": "gcd_tb.vcd", "format": "vcd"},
    "clock": {"signal": "clk", "edge": "rising"},
    "reset": {"signal": "rst_n", "active": "low"},
    "signals": None,
    "store": {"path": ".klt/wave/gcd_tb.klwave"},
}

_BUILD_RESPONSE_DOC = {
    "schema_version": 1,
    "trace": {"path": "gcd_tb.vcd", "format": "vcd", "size_bytes": 184320000},
    "store": {
        "path": ".klt/wave/gcd_tb.klwave",
        "size_bytes": 6291456,
        "content_hash": "sha256:" + "a" * 64,
    },
    "clock": {"signal": "tb.clk", "edge": "rising", "period_ns": 2.0},
    "reset": {
        "signal": "tb.rst_n",
        "active": "low",
        "release": {"time_ns": 40.0, "cycle": 0},
    },
    "timescale": {"unit": "ns", "value": 1},
    "time_range": {
        "from": {"time_ns": 0.0, "cycle": None},
        "to": {"time_ns": 20000.0, "cycle": 9980},
    },
    "signal_count": 128,
    "value_change_count": 458213,
    "provenance": {
        "klt_version": "0.4.2",
        "klayout_version": None,
        "pdk": None,
        "deck": None,
        "input": {"content_hash": "sha256:" + "b" * 64},
    },
}

_QUERY_REQUEST_DOC = {
    "schema": "klt.wave_query.request/1",
    "store": ".klt/wave/gcd_tb.klwave",
    "ops": [
        {"op": "value", "signal": "dut.o_valid", "at": {"cycle": 42}},
        {
            "op": "find",
            "signal": "dut.o_valid",
            "match": {"value": "1"},
            "occurrence": "first",
            "window": {"from": {"cycle": 0}},
            "predicate": {"max_cycle": 100},
        },
    ],
}

_QUERY_RESPONSE_DOC = {
    "schema_version": 1,
    "store": {
        "path": ".klt/wave/gcd_tb.klwave",
        "content_hash": "sha256:" + "c" * 64,
    },
    "status": "unsatisfied",
    "results": [
        {
            "op": "value",
            "signal": "dut.o_valid",
            "at": {"time_ns": 84.0, "cycle": 42},
            "value": "1",
        },
        {
            "op": "find",
            "signal": "dut.o_valid",
            "found": True,
            "at": {"time_ns": 114.0, "cycle": 57},
            "satisfied": False,
        },
    ],
    "provenance": {
        "klt_version": "0.4.2",
        "klayout_version": None,
        "pdk": None,
        "deck": None,
        "input": {"content_hash": "sha256:" + "c" * 64},
    },
}


def test_wave_build_request_doc_validates():
    jsonschema.validate(
        _BUILD_REQUEST_DOC, _load_schema("wave-build-request.schema.json")
    )


def test_wave_build_response_doc_validates():
    jsonschema.validate(
        _BUILD_RESPONSE_DOC, _load_schema("wave-build-response.schema.json")
    )


def test_wave_query_request_doc_validates():
    jsonschema.validate(
        _QUERY_REQUEST_DOC, _load_schema("wave-query-request.schema.json")
    )


def test_wave_query_response_doc_validates():
    jsonschema.validate(
        _QUERY_RESPONSE_DOC, _load_schema("wave-query-response.schema.json")
    )


def test_wave_build_request_empty_signals_array_rejected():
    """AC (contract section 4): a non-null, empty `signals` array is a
    request error -- enforced at the schema level via `minItems: 1` on the
    non-null form."""
    bad = {**_BUILD_REQUEST_DOC, "signals": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _load_schema("wave-build-request.schema.json"))


def test_wave_query_request_empty_ops_rejected():
    """AC (contract section 5): an empty `ops` array is a request error."""
    bad = {**_QUERY_REQUEST_DOC, "ops": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _load_schema("wave-query-request.schema.json"))


def test_wave_query_request_sample_op_rejects_predicate():
    """AC (contract section 5): `predicate` is undefined for `sample`/`wave`
    -- a request naming one is rejected (each op variant's
    `additionalProperties: false` excludes the undeclared `predicate`
    key)."""
    bad = {
        "store": ".klt/wave/gcd_tb.klwave",
        "ops": [
            {
                "op": "sample",
                "signals": ["dut.a"],
                "edge_of": "clk",
                "predicate": {"expect": True},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _load_schema("wave-query-request.schema.json"))


# ---------------------------------------------------------------------------
# Wrapper unit tests: `_binary_path()`-equivalent degrade-cleanly error path
# ---------------------------------------------------------------------------


def test_binary_path_raises_actionable_error_when_unbuilt(monkeypatch):
    monkeypatch.setattr(wave.os.path, "isfile", lambda _path: False)
    with pytest.raises(WaveError, match="cargo build --release"):
        wave._binary_path()


def test_binary_path_raises_actionable_error_mentions_native_wave(monkeypatch):
    monkeypatch.setattr(wave.os.path, "isfile", lambda _path: False)
    with pytest.raises(WaveError, match="native/wave"):
        wave._binary_path()


def test_run_wave_build_raises_wave_error_when_binary_unbuilt(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wave,
        "_binary_path",
        lambda: (_ for _ in ()).throw(
            WaveError(
                "the klt-wave binary is not built -- from a repo checkout, "
                "run `cargo build --release` inside native/wave/"
            )
        ),
    )
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")
    with pytest.raises(WaveError, match="not built"):
        run_wave_build(str(request_path))


def test_run_wave_query_raises_wave_error_when_binary_unbuilt(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wave,
        "_binary_path",
        lambda: (_ for _ in ()).throw(
            WaveError(
                "the klt-wave binary is not built -- from a repo checkout, "
                "run `cargo build --release` inside native/wave/"
            )
        ),
    )
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")
    with pytest.raises(WaveError, match="not built"):
        run_wave_query(str(request_path))


def test_run_wave_build_raises_wave_error_on_launch_failure(monkeypatch, tmp_path):
    """`_invoke`'s own `OSError` handling (mirrors `techmap.py`'s
    identical convention) -- a binary that resolves but cannot actually be
    launched (e.g. permission denied) is also a clean `WaveError`, never an
    uncaught `OSError`."""
    monkeypatch.setattr(wave, "_binary_path", lambda: "/nonexistent/klt-wave")

    def _raise_oserror(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(wave.subprocess, "run", _raise_oserror)
    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")
    with pytest.raises(WaveError, match="could not launch"):
        run_wave_build(str(request_path))


# ---------------------------------------------------------------------------
# End-to-end integration test: real canary trace, real binary
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.skipif(
    not HAVE_CARGO, reason="cargo is not installed on this machine"
)


def _wave_binary() -> str:
    """Build (debug profile -- functional correctness only, no lto) and
    return the real `klt-wave` binary path, mirroring
    `test_techmap_equiv_gate.py::_techmap_binary`'s own convention."""
    binary = Path(wave._WAVE_CRATE_DIR) / "target" / "debug" / "klt-wave"
    if not binary.is_file():
        subprocess.run(["cargo", "build"], cwd=wave._WAVE_CRATE_DIR, check=True)
    assert binary.is_file(), f"cargo build did not produce {binary}"
    return str(binary)


@pytest.mark.skipif(not HAVE_CARGO, reason="cargo is not installed on this machine")
def test_build_then_query_modexp_canary_counts_done_handshakes(tmp_path, monkeypatch):
    """End-to-end AC: trace a real canary testbench (Icarus-simulated
    `modexp.v`), `klt wave build` the resulting VCD, `klt wave query` it,
    and assert a known handshake count -- `tb_modexp.done` rises exactly 3
    times, one per `run_case` vector in `native/wave/tests/fixtures/
    README.md`'s regeneration recipe (modexp(2,10,100)=24,
    modexp(3,7,50)=37, modexp(7,13,11)=2)."""
    assert MODEXP_FIXTURE.is_file(), f"missing committed fixture: {MODEXP_FIXTURE}"

    binary = _wave_binary()
    debug_dir = Path(binary).parent
    # `_binary_path()` prefers a release build; force it to resolve the
    # debug build this test just ensured exists, without requiring a full
    # (slower) release compile in CI.
    monkeypatch.setattr(
        wave,
        "_binary_path",
        lambda: str(debug_dir / "klt-wave"),
    )

    store_path = tmp_path / "modexp_canary.klwave"
    build_request = {
        "schema": "klt.wave_build.request/1",
        "trace": {"path": str(MODEXP_FIXTURE), "format": "vcd"},
        "clock": {"signal": "tb_modexp.clk", "edge": "rising"},
        "reset": {"signal": "tb_modexp.rst_n", "active": "low"},
        "signals": None,
        "store": {"path": str(store_path)},
    }
    build_request_path = tmp_path / "build_request.json"
    build_request_path.write_text(json.dumps(build_request), encoding="utf-8")

    build_response = run_wave_build(str(build_request_path))

    assert build_response["schema_version"] == 1
    assert build_response["trace"]["format"] == "vcd"
    assert build_response["signal_count"] == 42
    assert build_response["value_change_count"] == 2106
    assert build_response["clock"]["period_ns"] == 10.0
    assert build_response["reset"]["release"]["time_ns"] == 25.0
    # The Python wrapper overrides the binary's own crate version with the
    # installed `klayout-tools` build identity -- see `wave.py`'s own
    # docstring, "provenance.klt_version override".
    from klayout_tools.build_identity import build_version

    assert build_response["provenance"]["klt_version"] == build_version()

    jsonschema.validate(build_response, _load_schema("wave-build-response.schema.json"))

    query_request = {
        "schema": "klt.wave_query.request/1",
        "store": str(store_path),
        "ops": [
            {
                "op": "count",
                "signal": "tb_modexp.done",
                "match": {"edge": "rising"},
                "predicate": {"min": 3, "max": 3},
            }
        ],
    }
    query_request_path = tmp_path / "query_request.json"
    query_request_path.write_text(json.dumps(query_request), encoding="utf-8")

    query_response = run_wave_query(str(query_request_path))

    assert query_response["status"] == "ok"
    assert query_response["results"][0]["count"] == 3
    assert query_response["results"][0]["satisfied"] is True

    jsonschema.validate(query_response, _load_schema("wave-query-response.schema.json"))
