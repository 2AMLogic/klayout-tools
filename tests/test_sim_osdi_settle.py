"""A *late* host-default yield must re-settle `options.osdi_preload`
(issue #5492, `src/klayout_tools/sim_osdi_settle.py`).

The bug: a request that declares a runner-resolved entry (a literal
`$PDK_ROOT/<rel>` / `${PDK_ROOT}/<rel>`, or a path under `models.pdk_root` --
issue #3819) lost that entry whenever the backend came from
`$KLT_SIM_BACKEND` and a yield *after* the one-shot settling stepped the run
back to `local`. The deck that actually ran here carried no `pre_osdi` line
for it, the report said `backend.resolved: "local"` while still listing the
entry under `environment.osdi_preload_runner_resolved`, and the only symptom
was an ngspice `Unknown model type` error deep in a per-corner log.

Two yields run after the settling and are both covered here: the resume
yield in `sim._prepare_sim_request` and the single-unit yield in
`sim._run_sim_corners`. The settling itself (the off-host half, and the
explicit-backend cases this must never second-guess) is covered beside the
rest of `options.osdi_preload` in `tests/test_sim.py`.

No SPICE runs: ngspice is stubbed by `_capture_decks` and the batch backend
is replaced by a stub that fails the test if it is ever reached.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim

# `_capture_decks` replaces the shared `subprocess.run`, which would otherwise
# also swallow the build-provenance Git probe; keep that one on the real
# runner (`tests/conftest.py`), as every other engine-stubbing module does.
pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_HOST_DEFAULT_LOG = (
    "  Measurements for Transient Analysis\n\nvout                =  1.00000e+00\n"
)


def _write_body(tmp_path: Path, name: str = "body.spice") -> Path:
    path = tmp_path / name
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_osdi(
    tmp_path: Path, name: str = "psp103.osdi", content: bytes = b""
) -> Path:
    path = tmp_path / name
    path.write_bytes(content or f"fake osdi {name}".encode())
    return path


def _osdi_request(tmp_path: Path, osdi_preload, **extra) -> Path:
    _write_body(tmp_path)
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        "options": {"osdi_preload": osdi_preload},
    }
    request.update(extra)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _capture_decks(monkeypatch) -> list[str]:
    """Stub ngspice, recording the text of every deck it is handed."""
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        decks.append(Path(cmd[cmd.index("-b") + 1]).read_text())
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(_HOST_DEFAULT_LOG)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


# Issue #5492: a *late* host-default yield (the single-unit yield in
# `run_sim`, the resume yield in `_prepare_sim_request`) lands the run on
# this host after `_finalize_osdi_preload` already settled the list against
# the off-host backend. The runner-resolved entries must be re-settled, or
# the local deck carries no `pre_osdi` line for them and ngspice fails with
# `Unknown model type` deep in a per-corner log.


def _runner_resolved_osdi_request(tmp_path, monkeypatch, *, pin, stage, extra=None):
    """A single-corner request whose one `osdi_preload` entry is
    runner-resolved -- via a literal `$PDK_ROOT/` prefix (pin=None/"${...}")
    or via `models.pdk_root` (pin="models")."""
    pdk_root = tmp_path / "pdkroot"
    (pdk_root / "ihp" / "osdi").mkdir(parents=True)
    osdi = pdk_root / "ihp" / "osdi" / "psp103.osdi"
    osdi.write_bytes(b"fake osdi")
    options = {"stage_model_inputs": True} if stage else {}
    if pin == "models":
        monkeypatch.delenv("PDK_ROOT", raising=False)
        entry = str(osdi)
        extra_kwargs = {"models": {"pdk_root": str(pdk_root)}}
    else:
        monkeypatch.setenv("PDK_ROOT", str(pdk_root))
        entry = f"{pin}/ihp/osdi/psp103.osdi"
        extra_kwargs = {}
    extra_kwargs.update(extra or {})
    request = _osdi_request(tmp_path, [entry], **extra_kwargs)
    doc = json.loads(request.read_text())
    doc["options"].update(options)
    request.write_text(json.dumps(doc))
    return request, str(osdi), entry


@pytest.mark.parametrize("pin", ["$PDK_ROOT", "${PDK_ROOT}", "models"])
@pytest.mark.parametrize("stage", [False, True])
def test_run_sim_single_unit_host_default_resettles_runner_resolved_osdi(
    tmp_path, monkeypatch, pin, stage
):
    """A one-corner run under `$KLT_SIM_BACKEND=batch` steps back to `local`
    *after* the OSDI list was settled off-host -- the entries must be
    resolved and emitted, exactly as `--backend local` does."""

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError("the batch backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, "batch", must_not_dispatch)
    monkeypatch.setattr(sim, "_run_batch_fleet", must_not_dispatch)
    request, osdi, _entry = _runner_resolved_osdi_request(
        tmp_path, monkeypatch, pin=pin, stage=stage
    )

    decks = _capture_decks(monkeypatch)
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    report = sim.run_sim(str(request))
    monkeypatch.delenv(sim.BACKEND_ENV)
    explicit_decks = _capture_decks(monkeypatch)
    explicit = sim.run_sim(str(request), backend="local")

    assert report["environment"]["backend"]["resolved"] == "local"
    assert [d for d in decks if f"pre_osdi {osdi}" in d] == decks
    # Byte-identical to what the same request produces with --backend local.
    assert decks == explicit_decks
    environment = report["environment"]
    assert "osdi_preload_runner_resolved" not in environment
    assert environment["osdi_preload"] == explicit["environment"]["osdi_preload"]


@pytest.mark.parametrize("stage", [False, True])
def test_run_sim_single_unit_host_default_checks_runner_resolved_osdi_exists(
    tmp_path, monkeypatch, stage
):
    """A missing image-baked `.osdi` fails with the same `SimError` a
    `--backend local` run raises -- not an ngspice model error per corner."""

    def must_not_run(*args, **kwargs):
        raise AssertionError("no corner may be dispatched")

    monkeypatch.setattr(sim.subprocess, "run", must_not_run)
    monkeypatch.setitem(sim._BACKENDS, "batch", must_not_run)
    request, osdi, _entry = _runner_resolved_osdi_request(
        tmp_path, monkeypatch, pin="$PDK_ROOT", stage=stage
    )
    os.remove(osdi)
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    with pytest.raises(sim.SimError, match="osdi_preload file not found"):
        sim.run_sim(str(request))


@pytest.mark.parametrize("stage", [False, True])
def test_run_sim_single_unit_host_default_keeps_mixed_osdi_declared_order(
    tmp_path, monkeypatch, stage
):
    """A host-local entry and a runner-resolved one keep declared order in
    the deck on the single-unit path."""

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError("the batch backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, "batch", must_not_dispatch)
    pdk_root = tmp_path / "pdkroot"
    (pdk_root / "ihp" / "osdi").mkdir(parents=True)
    baked = pdk_root / "ihp" / "osdi" / "a.osdi"
    baked.write_bytes(b"fake osdi a")
    monkeypatch.setenv("PDK_ROOT", str(pdk_root))
    local = _write_osdi(tmp_path)
    request = _osdi_request(tmp_path, ["$PDK_ROOT/ihp/osdi/a.osdi", "psp103.osdi"])
    doc = json.loads(request.read_text())
    doc["options"]["stage_model_inputs"] = stage
    request.write_text(json.dumps(doc))
    decks = _capture_decks(monkeypatch)
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    sim.run_sim(str(request))

    assert decks
    for deck in decks:
        pre = [ln for ln in deck.splitlines() if ln.startswith("pre_osdi")]
        assert pre == [f"pre_osdi {baked}", f"pre_osdi {local}"]


def test_run_sim_resume_host_default_resettles_runner_resolved_osdi(
    tmp_path, monkeypatch
):
    """The resume yield (`options.resume` steps an env-sourced off-host
    default back to `local`) re-settles the list the same way -- several
    corners, so the single-unit yield is not what rescues it."""

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError("the batch backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, "batch", must_not_dispatch)
    request, osdi, _entry = _runner_resolved_osdi_request(
        tmp_path,
        monkeypatch,
        pin="$PDK_ROOT",
        stage=False,
        extra={"corners": {"temperature_c": [-40, 27, 125]}},
    )
    doc = json.loads(request.read_text())
    doc["options"]["resume"] = True
    request.write_text(json.dumps(doc))
    decks = _capture_decks(monkeypatch)
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    report = sim.run_sim(str(request))

    assert report["environment"]["backend"]["resolved"] == "local"
    assert len(decks) == 3
    assert all(f"pre_osdi {osdi}" in deck for deck in decks)
    assert "osdi_preload_runner_resolved" not in report["environment"]


def test_run_sim_single_unit_host_default_applies_the_local_osdi_abi_check(
    tmp_path, monkeypatch
):
    """`_check_local_osdi_abi` (issue #4438) sees the re-settled entries, so
    an `.osdi` the local ngspice cannot load is refused up front."""
    seen: list[tuple] = []

    def fake_check(osdi_preload, backend, engine, binary):
        seen.append((tuple(osdi_preload), backend))

    monkeypatch.setattr(sim, "_check_local_osdi_abi", fake_check)
    monkeypatch.setitem(
        sim._BACKENDS,
        "batch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no batch")),
    )
    request, osdi, _entry = _runner_resolved_osdi_request(
        tmp_path, monkeypatch, pin="$PDK_ROOT", stage=False
    )
    _capture_decks(monkeypatch)
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    sim.run_sim(str(request))

    assert ((osdi,), "local") in seen


# Issue #5562: the declared index of each runner-resolved entry is what the
# batch report joins the runner's own digest on (see
# `tests/test_sim_batch_osdi_digest.py`), so the settling must carry it, and
# every reported entry must carry an explicit `sha256` -- a present `null`
# says "unknown", where a missing field invites a consumer to substitute the
# submitting host's hash of a file the runner never read.


def test_finalize_osdi_preload_carries_the_declared_index():
    from klayout_tools import sim_osdi_settle

    options = {"osdi_preload": ["local.osdi", "$PDK_ROOT/a.osdi", "${PDK_ROOT}/b.osdi"]}

    preload, environment = sim_osdi_settle.finalize_osdi_preload(
        options,
        "/nonexistent",
        True,
        ("/abs/local.osdi",),
        {2: "env_var", 1: "pdk_root"},
    )

    assert preload == ("/abs/local.osdi",)
    assert environment == [
        {"entry": "$PDK_ROOT/a.osdi", "index": 1, "reason": "pdk_root"},
        {"entry": "${PDK_ROOT}/b.osdi", "index": 2, "reason": "env_var"},
    ]


def test_osdi_runner_environment_fields_report_an_explicit_null_digest():
    from klayout_tools import sim_osdi_settle

    settled = [{"entry": "$PDK_ROOT/a.osdi", "index": 0, "reason": "env_var"}]

    fields = sim_osdi_settle.osdi_runner_environment_fields(settled)

    assert fields == {
        "osdi_preload_runner_resolved": [
            {
                "entry": "$PDK_ROOT/a.osdi",
                "index": 0,
                "reason": "env_var",
                "sha256": None,
            }
        ]
    }
    # The settled list itself is not mutated, and an on-host run reports
    # nothing at all (`_settle_on_host` returns `[]`).
    assert "sha256" not in settled[0]
    assert sim_osdi_settle.osdi_runner_environment_fields([]) == {}
