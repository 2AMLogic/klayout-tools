"""Tests for ``options.complex_vectors`` -- declared AC vectors returned as a
sidecar artifact, locally and through the batch artifact install (issue #3337).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_batch, sim_complex_vectors

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

real_ngspice = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice not installed"
)

# Single-pole RC low-pass, fc = 1 kHz, driven by 1 V AC: v(out) = 1/(1+jf/fc).
_FC = 1e3


def _request(tmp_path: Path, options: dict | None = None, **extra) -> Path:
    (tmp_path / "body.spice").write_text(
        "V1 in 0 DC 0 AC 1\nR1 in out 1k\nC1 out 0 159.1549431n\n"
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "ac", "args": "dec 10 10 100k"},
                "options": options or {},
                **extra,
            }
        )
    )
    return path


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

_AC = {"kind": "ac", "args": "dec 10 1 1e6"}


def test_absent_option_declares_nothing():
    assert sim_complex_vectors.validate({}, _AC, "all") == ()


def test_selectors_are_normalised_and_deduplicated():
    got = sim_complex_vectors.validate(
        {"complex_vectors": ["V(OUT)", "v(in)", "v(out)"]}, _AC, "all"
    )
    assert got == ("v(out)", "v(in)")


@pytest.mark.parametrize(
    "value", ["v(out)", [], ["i(v1)"], ["v(a,b)"], ["v(out)-v(in)"], [5]]
)
def test_malformed_option_is_rejected(value):
    with pytest.raises(sim.SimError, match="complex_vectors"):
        sim_complex_vectors.validate({"complex_vectors": value}, _AC, "all")


def test_non_ac_analysis_is_rejected():
    with pytest.raises(sim.SimError, match='analysis.kind "ac"'):
        sim_complex_vectors.validate(
            {"complex_vectors": ["v(out)"]}, {"kind": "tran"}, "all"
        )


def test_save_mode_netlist_is_rejected_before_launch(tmp_path):
    request = _request(
        tmp_path, {"complex_vectors": ["v(out)"], "save_mode": "netlist"}
    )
    with pytest.raises(sim.SimError, match="save_mode"):
        sim.run_sim(str(request))


def test_refused_for_xyce(tmp_path):
    request = _request(tmp_path, {"complex_vectors": ["v(out)"]}, engine="xyce")
    with pytest.raises(sim.SimError, match="not supported for engine"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Extraction from a rawfile
# --------------------------------------------------------------------------- #


def _rawfile(rows: list[tuple[float, complex]], names=("__klt_cv_0",)) -> str:
    lines = [
        "Title: stub",
        "Plotname: AC Analysis",
        "Flags: complex",
        f"No. Variables: {1 + len(names)}",
        f"No. Points: {len(rows)}",
        "Variables:",
        "\t0\tfrequency\tfrequency",
        *(f"\t{i + 1}\t{n}\tvoltage" for i, n in enumerate(names)),
        "Values:",
    ]
    for i, (f, z) in enumerate(rows):
        lines.append(f" {i}\t{f!r},0.0")
        for _ in names:
            lines.append(f"\t{z.real!r},{z.imag!r}")
    return "\n".join(lines) + "\n"


def _extract(tmp_path, text, vectors=("v(out)",)):
    if text is not None:
        Path(sim_complex_vectors.rawfile_path(str(tmp_path))).write_text(text)
    return sim_complex_vectors.extract_corner(vectors, str(tmp_path))


def test_extract_writes_the_sidecar(tmp_path):
    rows = [(1.0, 1 - 2j), (10.0, 0.5 - 0.25j)]
    outcome = _extract(tmp_path, _rawfile(rows))
    assert outcome.diagnostics == []
    assert outcome.points == 2
    sidecar = json.loads(Path(outcome.path).read_text())
    assert sidecar["frequency_hz"] == [1.0, 10.0]
    assert sidecar["data"]["v(out)"] == {"real": [1.0, 0.5], "imag": [-2.0, -0.25]}


def test_absent_rawfile_is_an_error_naming_the_vector(tmp_path):
    outcome = _extract(tmp_path, None)
    assert outcome.path is None
    (diag,) = outcome.diagnostics
    assert diag["severity"] == "error"
    assert "[complex_vector_missing]" in diag["message"]
    assert "v(out)" in diag["message"]


def test_truncated_rawfile_is_an_error(tmp_path):
    text = _rawfile([(1.0, 1 + 0j), (2.0, 1 + 0j)]).replace(
        "No. Points: 2", "No. Points: 3"
    )
    (diag,) = _extract(tmp_path, text).diagnostics
    assert "[complex_vector_raw_invalid]" in diag["message"]
    assert "v(out)" in diag["message"]


def test_non_finite_sample_is_an_error_naming_the_vector(tmp_path):
    text = _rawfile([(1.0, 1 + 0j), (2.0, complex(float("nan"), 0.0))])
    (diag,) = _extract(tmp_path, text).diagnostics
    assert "[complex_vector_non_finite]" in diag["message"]
    assert "v(out)" in diag["message"]


def test_vector_missing_from_the_rawfile_names_that_vector(tmp_path):
    text = _rawfile([(1.0, 1 + 0j)], names=("v(__klt_cv_1)",))
    (diag,) = _extract(tmp_path, text, vectors=("v(out)", "v(nope)")).diagnostics
    assert "[complex_vector_missing]" in diag["message"]
    assert "v(out)" in diag["message"]
    assert "v(nope)" not in diag["message"]


# --------------------------------------------------------------------------- #
# Real ngspice, local backend
# --------------------------------------------------------------------------- #


def _run(tmp_path, options):
    return sim.run_sim(str(_request(tmp_path, options)), backend="local")


@real_ngspice
def test_real_ngspice_sidecar_matches_the_closed_form(tmp_path):
    # keep_artifacts is off in the request: declared vectors force it on.
    report = _run(
        tmp_path,
        {"complex_vectors": ["v(out)", "v(in)"], "keep_artifacts": False},
    )

    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert corner["complex_vectors"] == {"names": ["v(out)", "v(in)"], "points": 41}
    sidecar = json.loads(Path(corner["artifacts"]["complex_vectors"]).read_text())
    assert sidecar["points"] == 41
    for f, re, im in zip(
        sidecar["frequency_hz"],
        sidecar["data"]["v(out)"]["real"],
        sidecar["data"]["v(out)"]["imag"],
        strict=True,
    ):
        expected = 1 / (1 + 1j * f / _FC)
        assert complex(re, im) == pytest.approx(expected, rel=1e-6, abs=1e-9)
    assert all(v == 1.0 for v in sidecar["data"]["v(in)"]["real"])
    # The arrays never ride the report itself.
    assert "frequency_hz" not in json.dumps(report)


@real_ngspice
def test_real_ngspice_unresolved_vector_errors_the_corner(tmp_path):
    report = _run(tmp_path, {"complex_vectors": ["v(out)", "v(nope)"]})

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    messages = [d["message"] for d in corner["diagnostics"]]
    assert any("v(nope)" in m and "complex_vector_missing" in m for m in messages)
    assert corner["artifacts"]["complex_vectors"] is None


@real_ngspice
def test_report_is_unchanged_without_the_option(tmp_path):
    report = _run(tmp_path, {})
    (corner,) = report["corners"]
    assert "complex_vectors" not in corner
    assert "complex_vectors" not in corner["artifacts"]


# --------------------------------------------------------------------------- #
# Batch: runner gate and artifact install
# --------------------------------------------------------------------------- #


def test_complex_vectors_is_a_registered_mandatory_runner_feature():
    request = {"options": {"complex_vectors": ["v(out)"]}}
    assert sim_batch._request_features(request) == ["complex_vectors"]
    assert "complex_vectors" in sim_batch._MANDATORY_RUNNER_FEATURES
    assert sim_batch._RUNNER_REQUEST_FEATURES["complex_vectors"][0] == (0, 7, 1)
    assert sim_batch._request_features({"options": {}}) == []


def test_old_or_unknown_runner_misses_the_feature():
    assert sim_batch._missing_runner_features(["complex_vectors"], "0.7.0") == [
        "complex_vectors"
    ]
    assert sim_batch._missing_runner_features(["complex_vectors"], "0.7.1") == []


@real_ngspice
def test_batch_artifact_install_returns_the_same_sidecar(tmp_path):
    """Mocked batch round trip: the worker's report and artifacts tree are
    collected, installed, and rewritten like a fleet job's; the sidecar reads
    back identical to the local one."""
    (tmp_path / "local").mkdir()
    local_report = _run(tmp_path / "local", {"complex_vectors": ["v(out)"]})
    (local_corner,) = local_report["corners"]
    local_path = Path(local_corner["artifacts"]["complex_vectors"])

    # What the worker wrote under its $EDA_OUTPUT_DIR/artifacts, as collected.
    remote_root = "/job/outputs/artifacts"
    collected = tmp_path / "collected"
    worker_dir = collected / "artifacts" / "c0"
    worker_dir.mkdir(parents=True)
    shutil.copy(local_path, worker_dir / "complex_vectors.json")
    corner = {
        "artifacts": {
            "log": None,
            "complex_vectors": f"{remote_root}/c0/complex_vectors.json",
        }
    }
    installed = tmp_path / "installed"

    sim_batch._install_batch_artifacts(
        [corner], collected_dir=str(collected), artifacts_dir=str(installed)
    )

    rewritten = Path(corner["artifacts"]["complex_vectors"])
    assert rewritten == installed / "c0" / "complex_vectors.json"
    assert json.loads(rewritten.read_text()) == json.loads(local_path.read_text())
