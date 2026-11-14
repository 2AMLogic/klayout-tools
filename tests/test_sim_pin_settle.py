"""A *late* host-default yield must decide which engine and PDK the
`engine_version` / `models.pdk_version` pins are checked against (issue
#5500, `src/klayout_tools/sim_pin_settle.py`).

Both pins are backend-dependent, and before this fix both were evaluated
against the backend as it stood *before* the last two host-default yields
(the resume yield in `sim._prepare_sim_request` and the single-unit yield in
`sim._run_sim_corners`, where the unit count is first known). Under
`$KLT_SIM_BACKEND=batch` with no `--backend` and no `request.backend`, a
yield to `local` therefore produced two opposite defects:

- `engine_version`: the local pre-check skipped it (the backend looked
  off-host) and the post-run report check skipped it too (the backend was
  by then `local`), so a declared pin was enforced by *neither* half and an
  unmet pin passed silently.
- `models.pdk_version`: the off-host refusal (`#3462`) fired against the
  pre-yield `batch`, so a single-corner request that would have run locally
  was refused outright -- a host default turning a valid request into a
  refusal, which is the thing the yields exist to prevent.

Sibling coverage: `tests/test_sim_osdi_settle.py` (issue #5492) does the same
for `options.osdi_preload` at the same two yield sites; the pins' ordinary
explicit-backend behaviour is covered beside the rest of each pin in
`tests/test_sim.py`.

No SPICE runs: ngspice is stubbed, and the batch backend is replaced by a
stub that fails the test if it is ever reached.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim

# The stub replaces the shared `subprocess.run`, which would otherwise also
# swallow the build-provenance Git probe; keep that one on the real runner.
pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_MEAS_LOG = (
    "  Measurements for Transient Analysis\n\nvout                =  1.00000e+00\n"
)

#: `open_pdks` revision recorded in the fake PDK's `SOURCES` file.
_PDK_SHA = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"

_THREE_CORNERS = {"corners": {"temperature_c": [-40, 27, 125]}}


def _request(tmp_path: Path, **extra) -> Path:
    """A one-corner request whose body needs no PDK to netlist."""
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    request: dict = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
    }
    request.update(extra)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _pdk_pin_request(tmp_path: Path, monkeypatch, pin, *, sources=True, **extra):
    """`_request` plus a `models.pdk_version` pin against a fake PDK tree
    whose `SOURCES` records `_PDK_SHA` (the same fixture shape
    `tests/test_sim.py`'s pin tests use)."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "pdkroot"
    variant = root / "sky130A"
    (variant / "libs.tech" / "ngspice").mkdir(parents=True)
    if sources:
        (variant / "SOURCES").write_text(f"open_pdks {_PDK_SHA}", encoding="utf-8")
    return _request(
        tmp_path,
        models={
            "pdk": "sky130A",
            "pdk_root": str(root),
            "pdk_version": pin,
        },
        **extra,
    )


class _Engine:
    """Stub ngspice: `--version` answers `banner` and is counted; a
    simulation call is recorded and answered with a passing measurement log
    (or refused outright when `simulate` is false)."""

    def __init__(self) -> None:
        self.versions = 0
        self.sims: list[list[str]] = []


def _fake_engine(monkeypatch, banner: str, *, simulate: bool = True) -> _Engine:
    engine = _Engine()

    def fake_run(cmd, *args, **kwargs):
        if "--version" in cmd:
            engine.versions += 1
            return fake_completed(banner)
        engine.sims.append(list(cmd))
        if not simulate:
            raise AssertionError("no simulation may run")
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(_MEAS_LOG)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return engine


def _no_offhost(monkeypatch) -> None:
    """Fail the test if anything is dispatched off this host."""

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError("the batch backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, "batch", must_not_dispatch)
    monkeypatch.setattr(sim, "_run_batch_fleet", must_not_dispatch)


def _host_default_batch(monkeypatch) -> None:
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")


def _resumed(tmp_path: Path, request: Path) -> Path:
    """Turn `request` into a multi-corner resumed request -- the other late
    yield, so the single-unit rule is not what rescues it."""
    doc = json.loads(request.read_text())
    doc.update(_THREE_CORNERS)
    doc.setdefault("options", {})["resume"] = True
    doc["options"]["artifacts_dir"] = str(tmp_path / "artifacts")
    request.write_text(json.dumps(doc))
    return request


# --------------------------------------------------------------------------- #
# engine_version: the pin was enforced by neither half on the yield path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["single_unit", "resume"])
def test_host_default_yield_enforces_unmet_engine_version_pin(
    tmp_path, monkeypatch, path
):
    """An unmet pin fails closed with `engine_version_unmet` before any
    simulation runs, exactly as `--backend local` does -- it used to pass
    silently."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-42 : Circuit level\n")
    request = _request(tmp_path, engine_version="44")
    if path == "resume":
        _resumed(tmp_path, request)
    _host_default_batch(monkeypatch)

    with pytest.raises(
        sim.SimError, match="requires ngspice 44; .*found ngspice 42"
    ) as exc:
        sim.run_sim(str(request))

    assert exc.value.code == "engine_version_unmet"
    assert engine.sims == []


@pytest.mark.parametrize("path", ["single_unit", "resume"])
def test_host_default_yield_passes_satisfied_engine_version_pin(
    tmp_path, monkeypatch, path
):
    """A satisfied pin runs locally and passes."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2 : Circuit level\n")
    request = _request(tmp_path, engine_version="44")
    expected_corners = 1
    if path == "resume":
        _resumed(tmp_path, request)
        expected_corners = 3
    _host_default_batch(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["backend"]["resolved"] == "local"
    assert report["status"] == "pass"
    assert len(engine.sims) == expected_corners


@pytest.mark.parametrize("pin", [{"engine_version": "44"}, {}])
def test_host_default_yield_adds_no_extra_engine_version_probe(
    tmp_path, monkeypatch, pin
):
    """The settling *replaces* the check the early site skipped rather than
    adding a second one: the yield path issues exactly the same number of
    `ngspice --version` probes as the same request with `--backend local`,
    pinned or not."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2 : Circuit level\n")
    request = _request(tmp_path, **pin)
    _host_default_batch(monkeypatch)

    sim.run_sim(str(request))
    yielded = engine.versions

    monkeypatch.delenv(sim.BACKEND_ENV)
    local = _fake_engine(monkeypatch, "** ngspice-44.2 : Circuit level\n")
    sim.run_sim(str(request), backend="local")

    assert yielded == local.versions


def test_multi_corner_host_default_still_checks_engine_version_off_host(
    tmp_path, monkeypatch
):
    """A run that really leaves this host keeps the behaviour it has today:
    no pre-submit probe (there is no runner to probe), and the pin enforced
    against the version the runner's report carried, after the run."""
    submitted: list[list[str]] = []

    def fake_batch(*, corner_points, **kwargs):
        submitted.append([point.corner_id for point in corner_points])
        return [], "42", {"provider": "stub-batch"}

    monkeypatch.setitem(sim._BACKENDS, "batch", fake_batch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _request(tmp_path, engine_version="44", **_THREE_CORNERS)
    _host_default_batch(monkeypatch)

    with pytest.raises(
        sim.SimError, match="requires ngspice 44; the batch runner's report"
    ) as exc:
        sim.run_sim(str(request))

    assert exc.value.code == "engine_version_unmet"
    assert len(submitted) == 1  # the run was submitted, then checked
    assert engine.versions == 0  # no local probe for an off-host run


# --------------------------------------------------------------------------- #
# models.pdk_version: a host default must not turn a valid request into a
# refusal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["single_unit", "resume"])
def test_host_default_yield_accepts_matching_pdk_version_pin(
    tmp_path, monkeypatch, path
):
    """A matching pin runs locally -- no `#3462` refusal, which is what the
    pre-yield `batch` used to produce."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, _PDK_SHA[:12])
    if path == "resume":
        _resumed(tmp_path, request)
    _host_default_batch(monkeypatch)

    # Got past the pin check and reached the local engine (the same signal
    # `tests/test_sim.py::test_pdk_version_match_proceeds_to_simulation` uses).
    with pytest.raises(AssertionError, match="no simulation may run"):
        sim.run_sim(str(request))

    assert engine.sims


@pytest.mark.parametrize("path", ["single_unit", "resume"])
def test_host_default_yield_rejects_mismatching_pdk_version_pin(
    tmp_path, monkeypatch, path
):
    """A mismatching pin fails closed with `pdk_version_unmet` before any
    simulation runs -- the local comparison, not the off-host refusal."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, "c6d73a35f524070e")
    if path == "resume":
        _resumed(tmp_path, request)
    _host_default_batch(monkeypatch)

    with pytest.raises(sim.SimError, match="requires 'c6d73a35f524070e'") as exc:
        sim.run_sim(str(request))

    assert exc.value.code == "pdk_version_unmet"
    assert engine.sims == []


def test_host_default_yield_fails_closed_on_unknown_pdk_identity(tmp_path, monkeypatch):
    """The deferred comparison fails closed the same way the early one does
    when the installed revision cannot be identified."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, _PDK_SHA, sources=False)
    _host_default_batch(monkeypatch)

    with pytest.raises(sim.SimError, match="pinned PDK unavailable") as exc:
        sim.run_sim(str(request))

    assert exc.value.code == "pdk_version_unmet"
    assert engine.sims == []


def test_malformed_pdk_version_pin_still_refused_early(tmp_path, monkeypatch):
    """Deferring the *comparison* does not defer validating the pin's value:
    a non-string pin is refused under a host default too."""
    _no_offhost(monkeypatch)
    _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _request(tmp_path, models={"pdk_version": 5})
    _host_default_batch(monkeypatch)

    with pytest.raises(sim.SimError, match="non-empty string"):
        sim.run_sim(str(request))


def test_multi_corner_host_default_still_refuses_pdk_version_off_host(
    tmp_path, monkeypatch
):
    """A multi-corner request without `resume` stays on `batch`, so the
    deferred `#3462` refusal is due -- with the unchanged message, no
    `error.code`, and before anything is submitted."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, _PDK_SHA[:12], **_THREE_CORNERS)
    _host_default_batch(monkeypatch)

    with pytest.raises(sim.SimError, match="#3462") as exc:
        sim.run_sim(str(request))

    assert exc.value.code is None
    assert engine.sims == []


@pytest.mark.parametrize("backend", ["remote", "batch"])
def test_explicit_off_host_backend_still_refuses_pdk_version_pin(
    tmp_path, monkeypatch, backend
):
    """An explicitly named backend is never a host default, so it is refused
    exactly as early as before -- including a single corner, where a host
    default would now yield to `local`."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, _PDK_SHA)

    with pytest.raises(sim.SimError, match="#3462") as exc:
        sim.run_sim(str(request), backend=backend)

    assert exc.value.code is None
    assert engine.sims == []


def test_request_backend_field_still_refuses_pdk_version_pin(tmp_path, monkeypatch):
    """`request.backend` is an explicit choice too, not a host default, so a
    single-corner request carrying it is refused rather than yielded."""
    _no_offhost(monkeypatch)
    engine = _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, _PDK_SHA, backend="batch")
    _host_default_batch(monkeypatch)

    with pytest.raises(sim.SimError, match="#3462") as exc:
        sim.run_sim(str(request))

    assert exc.value.code is None
    assert engine.sims == []


def test_batch_set_preflight_still_refuses_pdk_version_pin(tmp_path, monkeypatch):
    """`--batch-set`'s preflight calls `_prepare_sim_request` with
    `backend="batch"` explicitly and never reaches `_run_sim_corners`, so the
    deferral must leave it refusing as it does today."""
    from klayout_tools import sim_batch_plan

    _no_offhost(monkeypatch)
    _fake_engine(monkeypatch, "** ngspice-44.2\n", simulate=False)
    request = _pdk_pin_request(tmp_path, monkeypatch, _PDK_SHA)
    _host_default_batch(monkeypatch)

    with pytest.raises(sim.SimError, match="#3462"):
        sim_batch_plan.preflight_batch_request(str(request))
