"""Issue #5321: runner models_lib hash comparison."""

from klayout_tools import sim_batch_features as sbm

# --- runner_models_lib_sha256 / models_lib_comparison (issue #5321) ---------

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _inner(sha):
    return {"environment": {"models_lib_sha256": sha}}


def test_runner_models_lib_sha256_lifted_or_null():
    f = sbm.runner_models_lib_sha256
    assert f(_inner(_SHA_A)) == _SHA_A
    assert f(_inner(None)) is None
    assert f(_inner("zz")) is None
    assert f({"environment": "x"}) is None
    assert f(None) is None


def test_models_lib_comparison_statuses():
    cmp = sbm._compare_models_lib
    assert cmp(_SHA_A, _SHA_A) == {"status": "match"}
    mismatch = cmp(_SHA_A, _SHA_B)
    assert mismatch["status"] == "mismatch" and mismatch["warning"]
    assert cmp(None, _SHA_A)["status"] == "not_comparable"
    assert cmp(_SHA_A, None)["status"] == "not_comparable"
    assert cmp(_SHA_A, _SHA_A, staged=True)["reason"] == "models_staged"


def _attach(remote, scope):
    env = {"remote": remote, "models_lib_sha256": _SHA_A}
    env["models_lib_sha256_scope"] = scope
    sbm.attach_models_lib_comparison(env, "batch")


def test_attach_models_lib_comparison_single_fleet_and_scope():
    single = {"runner_models_lib_sha256": _SHA_A}
    sbm.attach_models_lib_comparison(
        {
            "remote": single,
            "models_lib_sha256": _SHA_A,
            "models_lib_sha256_scope": "client",
        },
        "batch",
    )
    assert single["models_lib_comparison"] == {"status": "match"}

    fleet = {
        "fleet": [
            {"runner_models_lib_sha256": _SHA_A},
            None,
            {"runner_models_lib_sha256": _SHA_B},
            {"runner_models_lib_sha256": None},
        ]
    }
    sbm.attach_models_lib_comparison(
        {
            "remote": fleet,
            "models_lib_sha256": _SHA_A,
            "models_lib_sha256_scope": "client",
        },
        "batch",
    )
    statuses = [e["models_lib_comparison"]["status"] for e in fleet["fleet"] if e]
    assert statuses == ["match", "mismatch", "not_comparable"]

    other = {"runner_models_lib_sha256": _SHA_B}
    sbm.attach_models_lib_comparison(
        {
            "remote": other,
            "models_lib_sha256": _SHA_A,
            "models_lib_sha256_scope": "local",
        },
        "batch",
    )
    assert other["models_lib_comparison"]["status"] == "not_comparable"


# --- text output / wiring (issue #5321 review feedback) ----------------------


def _text_report(models_cmp, worker=None):
    remote = {"worker_environment": worker if worker is not None else {}}
    if models_cmp is not None:
        remote["models_lib_comparison"] = models_cmp
    return {"environment": {"remote": remote}}


def test_print_worker_pdk_models_lib_lines(capsys):
    from klayout_tools.cli.sim_pdk_text import _print_worker_pdk

    _print_worker_pdk(_text_report({"status": "match"}))
    assert capsys.readouterr().out.splitlines() == ["models_lib: match"]

    _print_worker_pdk(
        _text_report({"status": "not_comparable", "reason": "models_staged"})
    )
    assert capsys.readouterr().out.splitlines() == [
        "models_lib: not_comparable (models_staged)"
    ]

    _print_worker_pdk(_text_report({"status": "mismatch", "warning": "differs"}))
    assert capsys.readouterr().out.splitlines() == [
        "models_lib: mismatch",
        "warning: differs",
    ]

    for absent in (None, "x", {}, {"status": ""}):
        _print_worker_pdk(_text_report(absent))
        assert "models_lib" not in capsys.readouterr().out


def test_collected_report_env_surfaces_runner_hash():
    env = sbm.collected_report_env({}, _inner(_SHA_A))
    assert env["runner_models_lib_sha256"] == _SHA_A
    env = sbm.collected_report_env({}, {"environment": {}})
    assert env["runner_models_lib_sha256"] is None
