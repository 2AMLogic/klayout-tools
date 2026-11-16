"""Direct tests for the helpers the batch modules share via ``_batch_common``
(``job_timeout_seconds``, ``rewrite_runner_paths``) and for the collected-report
error branches in ``extract_batch`` / ``place_and_route_batch`` that those
helpers' callers keep (issue #5238 moved the helpers; the coverage ratchet
counts them per module)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import _batch_common, _provenance, sim_batch
from klayout_tools import extract_batch as eb
from klayout_tools import netlist_batch as nb
from klayout_tools import place_and_route_batch as par_batch
from klayout_tools import signoff_generator_batch as sgb
from klayout_tools import sim_commands as sc
from klayout_tools.extract import ExtractError
from klayout_tools.sim import SimError


def _write_report(collected: Path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (collected / sim_batch.BATCH_REPORT_FILENAME).write_text(text, encoding="utf-8")


# --- _batch_common.job_timeout_seconds ------------------------------------- #


def test_job_timeout_adds_the_per_job_overhead():
    expected = int(sim_batch._default_batch_job_timeout_s(1, 100.0) + 0.999999)
    got = _batch_common.job_timeout_seconds(100.0)
    assert isinstance(got, int)
    assert got > 100
    assert got in (expected, expected - 1)


def test_job_timeout_is_monotonic_and_ceilinged():
    assert _batch_common.job_timeout_seconds(10.5) >= _batch_common.job_timeout_seconds(
        10
    )


# --- _batch_common.rewrite_runner_paths ------------------------------------- #


def test_rewrite_reroots_exact_and_prefixed_strings():
    out = _batch_common.rewrite_runner_paths(
        ["/run/a", "/run/a/x.gds", "/run/ab", "/other"], ["/run/a"], "/local/"
    )
    assert out == ["/local", "/local/x.gds", "/run/ab", "/other"]


def test_rewrite_recurses_into_dicts_and_passes_scalars_through():
    out = _batch_common.rewrite_runner_paths(
        {"p": "/run/a/f", "n": 3, "none": None, "nest": {"l": ["/run/a/g"]}},
        ["/run/a"],
        "/local",
    )
    assert out == {"p": "/local/f", "n": 3, "none": None, "nest": {"l": ["/local/g"]}}


def test_rewrite_prefers_the_first_matching_root():
    out = _batch_common.rewrite_runner_paths("/run/a/b/c", ["/run/a/b", "/run/a"], "/L")
    assert out == "/L/c"


# --- extract_batch._read_report -------------------------------------------- #


def test_extract_read_report_missing_file(tmp_path):
    with pytest.raises(ExtractError) as err:
        eb._read_report(str(tmp_path), "job1")
    assert err.value.code == _batch_common.BATCH_JOB_FAILED_CODE


def test_extract_read_report_runner_error_keeps_its_code(tmp_path):
    _write_report(
        tmp_path,
        {
            "schema_version": 1,
            "error": {"command": "extract", "message": "bad", "code": "deck_missing"},
        },
    )
    with pytest.raises(ExtractError) as err:
        eb._read_report(str(tmp_path), "job1")
    assert err.value.code == "deck_missing"
    assert "bad" in str(err.value)


def test_extract_read_report_runner_error_without_code_defaults(tmp_path):
    _write_report(
        tmp_path,
        {"schema_version": 1, "error": {"command": "extract", "message": "bad"}},
    )
    with pytest.raises(ExtractError) as err:
        eb._read_report(str(tmp_path), "job1")
    assert err.value.code == _batch_common.BATCH_JOB_FAILED_CODE


def test_extract_read_report_rejects_a_non_extract_report(tmp_path):
    _write_report(tmp_path, {"unrelated": True})
    with pytest.raises(ExtractError, match="not a `klt extract` report"):
        eb._read_report(str(tmp_path), "job1")


def test_extract_read_report_accepts_an_extract_report(tmp_path):
    _write_report(tmp_path, {"netlist_path": "/x"})
    assert eb._read_report(str(tmp_path), "job1") == {"netlist_path": "/x"}


def test_extract_timeout_must_be_positive():
    with pytest.raises(ExtractError, match="must be positive"):
        eb.run_extract_batch({}, "/tmp", output=None, timeout_s=0)


# --- place_and_route_batch._read_unit_report -------------------------------- #


def test_par_read_unit_report_missing_file(tmp_path):
    with pytest.raises(par_batch._UnitFailed) as err:
        par_batch._read_unit_report(str(tmp_path), "job1")
    assert err.value.code == par_batch.BATCH_JOB_FAILED_CODE


def test_par_read_unit_report_runner_error_keeps_its_code(tmp_path):
    _write_report(
        tmp_path, {"status": "error", "error": {"message": "nope", "code": "x_code"}}
    )
    with pytest.raises(par_batch._UnitFailed) as err:
        par_batch._read_unit_report(str(tmp_path), "job1")
    assert err.value.code == "x_code"
    assert "nope" in str(err.value)


@pytest.mark.parametrize(
    "payload", [{"status": "error"}, {"status": "error", "error": "str"}]
)
def test_par_read_unit_report_without_a_runner_error(tmp_path, payload):
    _write_report(tmp_path, payload)
    with pytest.raises(
        par_batch._UnitFailed, match="not a `klt place-and-route` report"
    ) as err:
        par_batch._read_unit_report(str(tmp_path), "job1")
    assert err.value.code == par_batch.BATCH_JOB_FAILED_CODE


def test_par_read_unit_report_accepts_an_ok_report(tmp_path):
    _write_report(tmp_path, {"status": "ok"})
    assert par_batch._read_unit_report(str(tmp_path), "job1") == {"status": "ok"}


# --- place_and_route_batch._declared_inputs / _local_deck_hash --------------- #


def test_par_declared_inputs_skips_malformed_macros_and_resolves_paths(tmp_path):
    netlist = tmp_path / "top.v"
    netlist.write_text("module top; endmodule\n")
    abs_lef = tmp_path / "abs.lef"
    request = {
        "netlist": "top.v",
        "macros": [
            "not-a-dict",
            {"lef": "rel.lef", "gds": ""},
            {"lef": str(abs_lef), "gds": None},
        ],
    }
    paths = par_batch._declared_inputs(request, str(tmp_path))
    assert paths == [
        str(netlist),
        str(tmp_path / "rel.lef"),
        str(abs_lef),
    ]


def test_par_declared_inputs_without_macros(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    assert par_batch._declared_inputs({"netlist": "top.v"}, str(tmp_path)) == [
        str(tmp_path / "top.v")
    ]


def test_par_local_deck_hash_is_none_when_the_deck_does_not_resolve(monkeypatch):
    def _boom(*args, **kwargs):
        raise par_batch.PlaceAndRouteError("no deck")

    monkeypatch.setattr(par_batch, "_resolve_liberty", _boom)
    assert par_batch._local_deck_hash("lib", "tt", None) is None


# --------------------------------------------------------------------------- #
# _batch_common.build_job_command / build_job_spec (issue #5654)
#
# Five backends assembled the same job prologue by hand and had drifted apart.
# These assert the properties the shared builder now guarantees for all of
# them, so a sixth backend cannot reintroduce any of the four divergences.
# --------------------------------------------------------------------------- #

#: Every backend below is built with the same inner timeout, so the
#: wall-clock-cap assertions compare like with like.
_TIMEOUT_S = 60.0


def _backends(client_version: str | None = "0.9.0") -> dict[str, object]:
    """``name -> () -> BatchJobSpec`` for every batch backend, through each
    module's real ``build_job_spec``. ``client_version=None`` makes all five
    fall back to ``_provenance._klt_version()`` (what ``place_and_route_batch``
    always does), so a test can patch one version for the whole set."""
    tree = nb.BatchTree(
        root="/repo",
        files=("design/block.sch",),
        total_bytes=1,
        schematic="design/block.sch",
        rcfile_arg=None,
        rcfile_pdk_relative=False,
    )
    approval = sgb.GeneratorApproval(
        id="gen",
        interpreter="python3",
        script="scripts/gen.py",
        script_sha256="sha256:" + "0" * 64,
        artifact="build/ota.spice",
        output="build/ota.spice",
        timeout_s=_TIMEOUT_S,
    )
    return {
        "extract": lambda: eb.build_job_spec(
            tarball_path="/t.tgz",
            pdk="sky130A",
            timeout_s=_TIMEOUT_S,
            no_netlist=False,
            client_version=client_version,
        ),
        "netlist": lambda: nb.build_job_spec(
            tree,
            tarball_path="/t.tgz",
            pdk="sky130A",
            timeout_s=_TIMEOUT_S,
            block=False,
            client_version=client_version,
        ),
        "place-and-route": lambda: par_batch.build_job_spec(
            request_name="request.json",
            unit={},
            tarball_path="/t.tgz",
            pdk_variant="sky130A",
            timeout_s=_TIMEOUT_S,
        ),
        "signoff-generator": lambda: sgb.build_job_spec(
            approval, (), tarball_path="/t.tgz", client_version=client_version
        ),
        "sim-commands": lambda: sc.build_job_spec(
            [
                {
                    "id": "u1",
                    "timeout_s": _TIMEOUT_S,
                    "steps": [["true"]],
                    "inputs": [],
                    "outputs": [],
                }
            ],
            tarball_path="/t.tgz",
            pdk="sky130A",
            client_version=client_version,
        ),
    }


def _commands() -> dict[str, str]:
    return {name: build().cmd for name, build in _backends().items()}


_HOME_FALLBACK = 'export HOME="${HOME:-$(mktemp -d)}"'


def test_every_batch_command_exports_a_fallback_home():
    """Divergence 2: the fleet runner can leave ``HOME`` unset, which killed
    xschem's Tcl init outright (``bf3d1ae10``). That fix landed in
    ``netlist_batch``'s copy of the prologue only; the shared prologue gives
    it to every backend."""
    for name, cmd in _commands().items():
        assert _HOME_FALLBACK in cmd, name


def test_the_fallback_home_is_exported_before_the_tree_is_unpacked():
    """``$HOME`` must be set before anything runs in the extracted tree, and
    the scratch dir it falls back to must be outside that tree."""
    for name, cmd in _commands().items():
        assert cmd.index(_HOME_FALLBACK) < cmd.index('tar -xzf "$EDA_INPUT_DIR/'), name


def test_renaming_the_tree_root_file_moves_every_writer_and_the_reader(
    tmp_path, monkeypatch
):
    """Divergence 1: ``netlist_batch`` and ``place_and_route_batch`` each
    declared their own ``BATCH_TREE_ROOT_FILENAME`` while the reader --
    ``_batch_common.read_runner_roots`` -- used ``_batch_common``'s, so
    renaming it in the one declared place silently broke runner-path
    rewriting for two of the three producers."""
    monkeypatch.setattr(_batch_common, "BATCH_TREE_ROOT_FILENAME", "renamed.txt")
    writers = ("extract", "netlist", "place-and-route")
    commands = _commands()
    for name in writers:
        assert '>"$EDA_OUTPUT_DIR/renamed.txt"' in commands[name], name
        assert "tree_root.txt" not in commands[name], name
    (tmp_path / "renamed.txt").write_text("/scratch/abc\n", encoding="utf-8")
    assert _batch_common.read_runner_roots(str(tmp_path)) == ["/scratch/abc"]


def test_an_unpinnable_client_version_becomes_each_modules_error_class(monkeypatch):
    """Divergence 4: the preflight raises ``SimError`` when the submitting
    client's version cannot be pinned. ``signoff_generator_batch`` left it
    untranslated and nothing on the ``klt signoff`` path caught it, so it
    escaped as a Python traceback instead of ``docs/json-contract.md``'s
    error envelope."""
    monkeypatch.setattr(_provenance, "_klt_version", lambda: "not a version!!")
    expected = {
        "extract": ExtractError,
        "netlist": nb.NetlistError,
        "place-and-route": par_batch.PlaceAndRouteError,
        "signoff-generator": sgb.GeneratorApprovalError,
        "sim-commands": SimError,
    }
    builders = _backends(client_version=None)
    assert set(builders) == set(expected)
    for name, err_cls in expected.items():
        with pytest.raises(err_cls, match="cannot be pinned"):
            builders[name]()


def test_the_job_cap_leaves_headroom_wherever_an_inner_timeout_exists():
    """Divergence 3: ``job_timeout_seconds`` is the default so the inner
    ``timeout`` always fires before the harness's outer one.
    ``klt place-and-route`` wraps its run in no inner ``timeout`` and
    ``request.batch.job_timeout_s`` is documented as the bound on one job
    (``docs/cli/place-and-route.md``), so it deliberately keeps the
    operator's number instead."""
    caps = {name: build().timeout_seconds for name, build in _backends().items()}
    headroom = _batch_common.job_timeout_seconds(_TIMEOUT_S)
    assert headroom > _TIMEOUT_S
    for name in ("extract", "netlist", "signoff-generator", "sim-commands"):
        assert caps[name] == headroom, name
    assert caps["place-and-route"] == int(_TIMEOUT_S)


def test_only_the_commands_job_manages_its_own_redirects_and_exit_status():
    """``klt sim``'s multi-unit command publishes each unit's stdout, stderr
    and exit code itself, so the job has no ``report.json`` and a failed unit
    is a reported outcome rather than a failed job. Every other backend exits
    with its run's own status."""
    commands = _commands()
    assert commands["sim-commands"].endswith("; exit 0")
    assert ' > "$EDA_OUTPUT_DIR/report.json"' not in commands["sim-commands"]
    for name, cmd in commands.items():
        if name == "sim-commands":
            continue
        assert cmd.endswith("; exit $rc"), name
        assert "; rc=$?; wait $! 2>/dev/null" in cmd, name
