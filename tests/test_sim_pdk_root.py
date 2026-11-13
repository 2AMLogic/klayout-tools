"""Tests for ``klt sim --pdk-root`` (issue #4758).

Split out of ``test_sim.py`` (file-size ratchet); shares its PDK-store helpers.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import pdk, sim, sim_batch, sim_pdk_ambiguity
from klayout_tools.cli import main
from klayout_tools.cli.sim_pdk_text import _print_pdk
from test_sim import (
    _isolate_pdk_search,
    _make_pdk_store,
    _stub_subprocess_run,
    _write_body,
    _write_request,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def _two_store_request(tmp_path, monkeypatch, **models):
    _write_body(tmp_path)
    ciel = _make_pdk_store(tmp_path, "ciel", stamp="open_pdks aaaa111")
    volare = _make_pdk_store(tmp_path, "volare", stamp="open_pdks bbbb222")
    _isolate_pdk_search(monkeypatch, [ciel, volare])
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "models": {
                "pdk": "sky130A",
                "lib": "libs.tech/ngspice/sky130.lib.spice",
                **models,
            },
        },
    )
    return request, ciel, volare


def test_cli_pdk_root_flag_pins_the_second_install(tmp_path, monkeypatch, capsys):
    """Issue #4758: `klt sim --pdk-root` is the CLI route the ambiguity
    warning names; it pins the root and silences the warning."""
    request, _ciel, volare = _two_store_request(tmp_path, monkeypatch)

    main(["sim", str(request), "--pdk-root", str(volare), "--format", "json"])

    captured = capsys.readouterr()
    block = json.loads(captured.out)["provenance"]["pdk"]
    assert block["version"] == "open_pdks bbbb222"
    assert "ambiguous_sources" not in block
    assert "installs provide PDK variant" not in captured.err


def test_cli_pdk_root_flag_wins_over_request_models_pdk_root(
    tmp_path, monkeypatch, capsys
):
    request, ciel, volare = _two_store_request(tmp_path, monkeypatch)
    # Point the request's own pin at ciel; the flag must still pick volare.
    data = json.loads(request.read_text())
    data["models"]["pdk_root"] = str(ciel)
    request.write_text(json.dumps(data))

    main(["sim", str(request), "--pdk-root", str(volare), "--format", "json"])

    block = json.loads(capsys.readouterr().out)["provenance"]["pdk"]
    assert block["version"] == "open_pdks bbbb222"


def test_cli_pdk_root_flag_relative_path_resolves_against_cwd(
    tmp_path, monkeypatch, capsys
):
    request, _ciel, volare = _two_store_request(tmp_path, monkeypatch)
    monkeypatch.chdir(volare.parent)

    main(["sim", str(request), "--pdk-root", volare.name, "--format", "json"])

    block = json.loads(capsys.readouterr().out)["provenance"]["pdk"]
    assert block["version"] == "open_pdks bbbb222"


def test_cli_pdk_root_flag_rejected_with_job_status(tmp_path, monkeypatch, capsys):
    request, _ciel, volare = _two_store_request(tmp_path, monkeypatch)

    code = main(["sim", str(request), "--pdk-root", str(volare), "--job-status", "j-1"])
    assert code == 2
    assert "--pdk-root has no effect with --job-status" in capsys.readouterr().err


def test_sim_ambiguity_text_names_a_cli_route(capsys):
    from klayout_tools.cli import sim_cmd

    report = {
        "provenance": {
            "pdk": {
                "name": "sky130A",
                "version": None,
                "source": "search root: a",
                "ambiguous_sources": ["search root: b"],
            }
        }
    }
    sim_cmd._print_pdk(report)
    assert "--pdk-root" in capsys.readouterr().out


# --- backend `batch`: the remedy the warning names (issue #5449) -------------


def _batch_dry_run_request(tmp_path, **extra):
    """A minimal batch-submittable request, as ``test_sim`` builds one for the
    dry-run tests. ``models`` is whatever the caller pins."""
    _write_body(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "monte_carlo": {"n": 3, "seed": 7, "vary": "both"},
            **extra,
        },
    )


def test_cli_batch_warning_names_pdk_root_env_not_the_refused_pins(
    tmp_path, monkeypatch, capsys
):
    """Issue #5449: on `batch` the #2564 remedy is a dead end -- both
    `--pdk-root` and `models.pdk_root` are refused there with
    `models_pdk_root_host_path`, so a submitter who takes the advice cannot
    submit. Name `$PDK_ROOT`, which pins the *client's* read and is never
    forwarded to the runner."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    request, ciel, volare = _two_store_request(tmp_path, monkeypatch)

    main(["sim", str(request), "--backend", "batch", "--dry-run"])

    err = capsys.readouterr().err
    # The hazard half is unchanged: both absolute roots are still named.
    assert "klt: warning: 2 installs provide PDK variant 'sky130A'" in err
    assert f"read {ciel}" in err
    assert f"skipped {volare}" in err
    # The remedy half names only the pin this backend accepts.
    assert "$PDK_ROOT" in err
    assert sim_pdk_ambiguity.BATCH_REMEDY in err
    assert "--pdk-root" not in err
    assert "models.pdk_root" not in err


def test_cli_local_backend_warning_text_is_unchanged(tmp_path, monkeypatch, capsys):
    """Issue #5449: only `batch` swaps the remedy. `local` keeps the #2564
    sentence byte-for-byte, so no existing caller's output moves."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    request, _ciel, _volare = _two_store_request(tmp_path, monkeypatch)

    main(["sim", str(request), "--backend", "local"])

    err = capsys.readouterr().err
    assert pdk.AMBIGUITY_REMEDY in err
    assert "$PDK_ROOT" not in err


def _pdk_text_report(backend):
    """A minimal report for `_print_pdk`: an ambiguous PDK block plus the
    `environment.backend` provenance (issue #2977) the renderer reads."""
    return {
        "provenance": {
            "pdk": {
                "name": "sky130A",
                "version": "open_pdks aaaa111",
                "source": "search root: /a",
                "ambiguous_sources": ["search root: /b"],
            }
        },
        "environment": {
            "backend": {"resolved": backend, "source": "flag", "requested": backend}
        },
    }


@pytest.mark.parametrize("backend", ["local", "local-parallel", "remote"])
def test_print_pdk_warning_line_is_unchanged_off_batch(backend, capsys):
    """Issue #5449: only `batch` moves. Every other backend keeps the #2564
    wording byte-for-byte."""
    _print_pdk(_pdk_text_report(backend))

    assert capsys.readouterr().out.splitlines()[-1] == (
        "pdk: WARNING: variant also installed under search root: /b -- "
        "resolution is first-match-wins; pin --pdk-root (or models.pdk_root) "
        "to choose deliberately"
    )


def test_print_pdk_warning_line_on_batch_names_pdk_root_env(capsys):
    """Issue #5449: the text line advised the same two refused pins the
    stderr warning did."""
    _print_pdk(_pdk_text_report("batch"))

    line = capsys.readouterr().out.splitlines()[-1]
    assert line == (
        "pdk: WARNING: variant also installed under search root: /b -- "
        "resolution is first-match-wins; set $PDK_ROOT to choose which "
        "install this client resolves; the runner reads its own, selected "
        "by models.pdk"
    )
    assert "--pdk-root" not in line
    assert "models.pdk_root" not in line


def test_print_pdk_warning_line_without_backend_provenance(capsys):
    """A report carrying no `environment.backend` block falls back to the
    default wording rather than raising (issue #5449)."""
    report = _pdk_text_report("batch")
    del report["environment"]

    _print_pdk(report)

    assert "pin --pdk-root" in capsys.readouterr().out


# --- backend `batch`: --dry-run applies the host-path refusal (issue #5449) --


def test_run_sim_dry_run_batch_refuses_host_models_pdk_root(tmp_path, monkeypatch):
    """`models.pdk_root` naming a directory on the *submitting* host is
    refused by a real submit (issue #4097) from `_resolve_batch_config`,
    which a dry run reaches only through `dry_run_runner_check` -- and that
    swallows this code. So the request used to dry-run clean and die on
    submit."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    local_root = tmp_path / "pdks"
    local_root.mkdir()
    request = _batch_dry_run_request(tmp_path, models={"pdk_root": str(local_root)})

    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(request), backend="batch", dry_run=True)

    assert excinfo.value.code == sim_batch.BATCH_MODELS_PDK_ROOT_HOST_PATH_CODE
    assert str(local_root) in str(excinfo.value)


def test_run_sim_dry_run_batch_pdk_root_refusal_matches_the_real_submit(
    tmp_path, monkeypatch
):
    """The dry run must raise the *same* code and message the submit path
    raises, not a dry-run-only paraphrase (issue #5449)."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    local_root = tmp_path / "pdks"
    local_root.mkdir()

    with pytest.raises(sim.SimError) as real_exc:
        sim_batch._validate_batch_pdk_root({"models": {"pdk_root": str(local_root)}})
    with pytest.raises(sim.SimError) as dry_exc:
        sim.run_sim(
            str(_batch_dry_run_request(tmp_path, models={"pdk_root": str(local_root)})),
            backend="batch",
            dry_run=True,
        )

    assert dry_exc.value.code == real_exc.value.code
    assert str(dry_exc.value) == str(real_exc.value)


def test_run_sim_dry_run_batch_pdk_root_refusal_needs_no_batch_config(
    tmp_path, monkeypatch
):
    """Issue #5449: unlike `dry_run_runner_check`, this refusal is a property
    of the request, so it fires on a host with no bucket and no provision
    script -- the same posture as the #3917 `models_lib` check."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    monkeypatch.delenv(sim_batch.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sim_batch.PROVISION_SCRIPT_ENV, raising=False)
    local_root = tmp_path / "pdks"
    local_root.mkdir()
    request = _batch_dry_run_request(tmp_path, models={"pdk_root": str(local_root)})

    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(request), backend="batch", dry_run=True)

    assert excinfo.value.code == sim_batch.BATCH_MODELS_PDK_ROOT_HOST_PATH_CODE


def test_run_sim_dry_run_batch_accepts_a_runner_only_pdk_root(tmp_path, monkeypatch):
    """A `models.pdk_root` that is *not* a directory here is assumed
    runner-valid and still dry-runs clean -- the #4097 refusal's own
    boundary, unchanged (issue #5449)."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    request = _batch_dry_run_request(tmp_path, models={"pdk_root": "/runner/only/pdks"})

    report = sim.run_sim(str(request), backend="batch", dry_run=True)

    assert report["dry_run"] is True
    assert report["status"] == "valid"


def test_run_sim_dry_run_batch_accepts_pdk_only_request(tmp_path, monkeypatch):
    """`models.pdk` alone names a variant the runner resolves in its own
    install; nothing host-local is pinned, so the dry run passes."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    request = _batch_dry_run_request(tmp_path, models={"pdk": "sky130A"})

    report = sim.run_sim(str(request), backend="batch", dry_run=True)

    assert report["status"] == "valid"


def test_cli_dry_run_batch_pdk_root_flag_is_refused(tmp_path, monkeypatch, capsys):
    """Issue #5449: `--pdk-root` is folded into `models.pdk_root` as an
    absolute path, so the flag form hits the same refusal -- which matters
    most here, because it is the very pin the ambiguity warning used to
    advise."""
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    local_root = tmp_path / "pdks"
    local_root.mkdir()
    request = _batch_dry_run_request(tmp_path, models={"pdk": "sky130A"})

    exit_code = main(
        [
            "sim",
            str(request),
            "--backend",
            "batch",
            "--dry-run",
            "--pdk-root",
            str(local_root),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["error"]["code"] == sim_batch.BATCH_MODELS_PDK_ROOT_HOST_PATH_CODE
    assert str(local_root) in envelope["error"]["message"]


# --- the warning keys on the *resolved* backend, not the host default -------


def test_cli_host_default_batch_single_unit_keeps_the_default_remedy(
    tmp_path, monkeypatch, capsys
):
    """A single-unit run under `$KLT_SIM_BACKEND=batch` yields back to
    `local` (`_yield_host_default_backend`), so `--pdk-root` /
    `models.pdk_root` *are* accepted and *do* silence the warning. Keying the
    remedy on the pre-yield backend named `$PDK_ROOT` as the only pin, which
    is false for that run, and disagreed with the `--format text` line
    (which reads `environment.backend.resolved`)."""
    request, _ciel, _volare = _two_store_request(tmp_path, monkeypatch)
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    main(["sim", str(request), "--format", "json"])

    captured = capsys.readouterr()
    backend_block = json.loads(captured.out)["environment"]["backend"]
    assert backend_block == {
        "resolved": "local",
        "source": "env",
        "requested": "batch",
    }
    assert pdk.AMBIGUITY_REMEDY in captured.err
    assert "$PDK_ROOT" not in captured.err
    assert sim_pdk_ambiguity.BATCH_REMEDY not in captured.err


def test_cli_host_default_batch_multi_unit_uses_the_batch_remedy(
    tmp_path, monkeypatch, capsys
):
    """The other half of the same invariant: a multi-unit run under the same
    host default *does* resolve to `batch`, so it keeps the `$PDK_ROOT`
    wording."""
    request, _ciel, _volare = _two_store_request(tmp_path, monkeypatch)
    data = json.loads(request.read_text())
    data["monte_carlo"] = {"n": 3, "seed": 7, "vary": "both"}
    request.write_text(json.dumps(data))
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    report = sim.run_sim(str(request), dry_run=True)

    assert report["backend"] == "batch"
    err = capsys.readouterr().err
    assert sim_pdk_ambiguity.BATCH_REMEDY in err
    assert pdk.AMBIGUITY_REMEDY not in err


def test_cli_host_default_batch_osdi_preload_keeps_the_default_remedy(
    tmp_path, monkeypatch, capsys
):
    """The OSDI-preload yield (`sim.py`'s second
    `_yield_host_default_backend`) is the same class: a host-local `.osdi`
    keeps the run here, so the remedy must stay the default one."""
    request, _ciel, _volare = _two_store_request(tmp_path, monkeypatch)
    osdi = tmp_path / "model.osdi"
    osdi.write_bytes(b"\x7fELF")
    data = json.loads(request.read_text())
    data["monte_carlo"] = {"n": 3, "seed": 7, "vary": "both"}
    data["options"] = {"osdi_preload": [str(osdi)]}
    request.write_text(json.dumps(data))
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    report = sim.run_sim(str(request), dry_run=True)

    assert report["backend"] == "local"
    err = capsys.readouterr().err
    assert pdk.AMBIGUITY_REMEDY in err
    assert "$PDK_ROOT" not in err


def test_run_sim_dry_run_host_default_batch_single_unit_allows_host_pdk_root(
    tmp_path, monkeypatch
):
    """The dry-run refusal added for `batch` must not fire on a run the host
    default yields back to `local`: there `models.pdk_root` is a perfectly
    good host path, and the report says `backend: local`."""
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    local_root = tmp_path / "pdks"
    local_root.mkdir()
    request = _batch_dry_run_request(tmp_path, models={"pdk_root": str(local_root)})
    data = json.loads(request.read_text())
    del data["monte_carlo"]
    request.write_text(json.dumps(data))

    report = sim.run_sim(str(request), dry_run=True)

    assert report["backend"] == "local"
    assert report["status"] == "valid"
