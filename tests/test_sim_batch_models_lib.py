"""Issues #5321 and #5457: runner model-identity comparison.

#5321 compares the ``models.lib`` hash the runner loaded with the client's.
#5457 adds the pair that answers the same question for a request that
declares no ``models.lib`` at all because every selected section names its
own library (issue #2522): ``runner_corner_section_libs`` and
``corner_section_libs_comparison``, plus the ``no_models_lib`` reason that
stops #5321's null-vs-null case reading as a runner fault.

Pure-dict tests throughout: no AWS, no ngspice, no fleet run. The lift reads
a field the runner already emits, so a runner older than #2522 simply reports
``null``.
"""

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


# --- runner_corner_section_libs / corner_section_libs_comparison (#5457) ----

_SHA_C = "c" * 64


def _entry(name, sha, **extra):
    """A client-side ``environment.corner_section_libs`` entry: the runner's
    lifted form is the same minus ``path``/``scope``."""
    return {"name": name, "path": None, "scope": "external", "sha256": sha, **extra}


def _inner_libs(entries):
    return {"environment": {"corner_section_libs": entries}}


def test_runner_corner_section_libs_lifts_name_and_sha_only():
    f = sbm.runner_corner_section_libs
    assert f(_inner_libs([_entry("nfet.lib", _SHA_A), _entry("res.lib", _SHA_B)])) == [
        {"name": "nfet.lib", "sha256": _SHA_A},
        {"name": "res.lib", "sha256": _SHA_B},
    ]
    # Upper-case digests normalise, exactly as `runner_models_lib_sha256` does.
    assert f(_inner_libs([_entry("nfet.lib", "A" * 64)])) == [
        {"name": "nfet.lib", "sha256": _SHA_A}
    ]


def test_runner_corner_section_libs_null_when_unreported_or_malformed():
    f = sbm.runner_corner_section_libs
    # A runner predating #2522 reports no key at all; an error envelope has no
    # `environment`; a truncated report may have a non-list or junk entries.
    assert f({"environment": {}}) is None
    assert f({"error": {"code": "batch_job_failed"}}) is None
    assert f({"environment": "x"}) is None
    assert f(None) is None
    assert f(_inner_libs([])) is None
    assert f(_inner_libs("nfet.lib")) is None
    assert f(_inner_libs([_entry("nfet.lib", "zz"), _entry("", _SHA_A), 7, None])) is (
        None
    )
    # A usable entry alongside junk still lifts.
    assert f(_inner_libs([7, _entry("nfet.lib", _SHA_A)])) == [
        {"name": "nfet.lib", "sha256": _SHA_A}
    ]


def test_collected_report_env_surfaces_runner_corner_section_libs():
    env = sbm.collected_report_env({}, _inner_libs([_entry("nfet.lib", _SHA_A)]))
    assert env["runner_corner_section_libs"] == [{"name": "nfet.lib", "sha256": _SHA_A}]
    # An ordinary request (no per-section libraries) gains the key as null.
    assert (
        sbm.collected_report_env({}, _inner(_SHA_A))["runner_corner_section_libs"]
        is None
    )


def test_corner_section_libs_comparison_statuses():
    cmp = sbm._compare_corner_section_libs
    client = [_entry("nfet.lib", _SHA_A), _entry("res.lib", _SHA_B)]
    runner = [
        {"name": "nfet.lib", "sha256": _SHA_A},
        {"name": "res.lib", "sha256": _SHA_B},
    ]
    assert cmp(runner, client) == {"status": "match"}
    # A shard handed only its own corners' libraries is a subset, not a
    # mismatch: membership, never count or position.
    assert cmp([runner[1]], client) == {"status": "match"}
    # Order is not compared either.
    assert cmp(list(reversed(runner)), client) == {"status": "match"}
    # Missing data on either side is never a proven mismatch.
    assert cmp(None, client) == {
        "status": "not_comparable",
        "reason": "runner_libs_unavailable",
    }
    assert cmp([], client)["reason"] == "runner_libs_unavailable"
    assert cmp(runner, None) == {
        "status": "not_comparable",
        "reason": "client_libs_unavailable",
    }
    assert cmp(runner, [])["reason"] == "client_libs_unavailable"
    # Staging replaces the image's models, so the comparison says nothing.
    assert cmp(runner, client, staged=True)["reason"] == "models_staged"


def test_corner_section_libs_comparison_mismatch_names_the_unmatched():
    cmp = sbm._compare_corner_section_libs
    client = [_entry("nfet.lib", _SHA_A), _entry("res.lib", _SHA_B)]
    verdict = cmp(
        [
            {"name": "nfet.lib", "sha256": _SHA_A},
            {"name": "res.lib", "sha256": _SHA_C},
            {"name": "cap.lib", "sha256": _SHA_C},
        ],
        client,
    )
    assert verdict["status"] == "mismatch"
    # Only the runner entries the client cannot account for, sorted.
    assert verdict["names"] == ["cap.lib", "res.lib"]
    assert "cap.lib, res.lib" in verdict["warning"]


def test_corner_section_libs_comparison_duplicate_basenames_match():
    """`name` is a basename, so two libraries in different directories can
    share one. Pair membership keeps that a match; comparing per name alone
    would have to pick one hash and call the other wrong."""
    cmp = sbm._compare_corner_section_libs
    client = [_entry("corner.lib", _SHA_A), _entry("corner.lib", _SHA_B)]
    runner = [
        {"name": "corner.lib", "sha256": _SHA_A},
        {"name": "corner.lib", "sha256": _SHA_B},
    ]
    assert cmp(runner, client) == {"status": "match"}
    # ... and a third, genuinely unresolved hash under the same basename is
    # still caught.
    verdict = cmp([*runner, {"name": "corner.lib", "sha256": _SHA_C}], client)
    assert verdict["status"] == "mismatch" and verdict["names"] == ["corner.lib"]


def _no_models_lib_env(remote, libs=None):
    """The environment of a request that declared no `models.lib` (#2522):
    `models_lib` absent, both hashes null by construction."""
    env = {
        "remote": remote,
        "models_lib": {"path": None, "scope": "absent"},
        "models_lib_sha256": None,
        "models_lib_sha256_scope": "client",
    }
    if libs is not None:
        env["corner_section_libs"] = libs
    return env


def test_models_lib_comparison_reports_no_models_lib_not_runner_fault():
    """The #5457 honesty fix: a request with no `models.lib` must not read as
    a runner or image problem the caller could fix by updating the image."""
    single = {"runner_models_lib_sha256": None}
    sbm.attach_models_lib_comparison(_no_models_lib_env(single), "batch")
    assert single["models_lib_comparison"] == {
        "status": "not_comparable",
        "reason": "no_models_lib",
    }
    # A request that HAS a models.lib and a runner that reported no hash
    # keeps the original reason -- that one IS fixable by updating the image.
    older = {"runner_models_lib_sha256": None}
    sbm.attach_models_lib_comparison(
        {
            "remote": older,
            "models_lib": {"path": None, "scope": "external"},
            "models_lib_sha256": _SHA_A,
            "models_lib_sha256_scope": "client",
        },
        "batch",
    )
    assert older["models_lib_comparison"]["reason"] == "runner_hash_unavailable"


def test_attach_corner_section_libs_comparison_single_and_fleet():
    client = [_entry("nfet.lib", _SHA_A), _entry("res.lib", _SHA_B)]
    single = {
        "runner_corner_section_libs": [{"name": "nfet.lib", "sha256": _SHA_A}],
        "runner_models_lib_sha256": None,
    }
    sbm.attach_models_lib_comparison(_no_models_lib_env(single, client), "batch")
    assert single["corner_section_libs_comparison"] == {"status": "match"}

    fleet = {
        "fleet": [
            # shard that saw a subset of the client's libraries
            {"runner_corner_section_libs": [{"name": "res.lib", "sha256": _SHA_B}]},
            None,
            # shard that loaded a library the client never resolved
            {"runner_corner_section_libs": [{"name": "res.lib", "sha256": _SHA_C}]},
            # shard predating #2522
            {"runner_corner_section_libs": None},
        ]
    }
    sbm.attach_models_lib_comparison(_no_models_lib_env(fleet, client), "batch")
    verdicts = [
        e["corner_section_libs_comparison"] for e in fleet["fleet"] if e is not None
    ]
    assert [v["status"] for v in verdicts] == ["match", "mismatch", "not_comparable"]
    assert verdicts[1]["names"] == ["res.lib"]
    assert verdicts[2]["reason"] == "runner_libs_unavailable"
    # Every member still gets the #5321 verdict alongside.
    assert all(
        e["models_lib_comparison"]["reason"] == "no_models_lib"
        for e in fleet["fleet"]
        if e is not None
    )


def test_attach_omits_corner_section_libs_comparison_for_an_ordinary_request():
    """A request with no per-section libraries on either side gains nothing
    but the null lift -- no new key, so an existing consumer sees an
    unchanged `environment.remote`."""
    single = {"runner_models_lib_sha256": _SHA_A, "runner_corner_section_libs": None}
    sbm.attach_models_lib_comparison(
        {
            "remote": single,
            "models_lib": {"path": None, "scope": "external"},
            "models_lib_sha256": _SHA_A,
            "models_lib_sha256_scope": "client",
        },
        "batch",
    )
    assert single["models_lib_comparison"] == {"status": "match"}
    assert "corner_section_libs_comparison" not in single

    # One side alone is enough to emit it: a client that resolved per-section
    # libraries against a runner too old to report them must say so, rather
    # than stay silent about an unanswered question.
    older = {"runner_models_lib_sha256": None, "runner_corner_section_libs": None}
    sbm.attach_models_lib_comparison(
        _no_models_lib_env(older, [_entry("nfet.lib", _SHA_A)]), "batch"
    )
    assert (
        older["corner_section_libs_comparison"]["reason"] == "runner_libs_unavailable"
    )


def test_print_worker_pdk_corner_section_libs_line(capsys):
    from klayout_tools.cli.sim_pdk_text import _print_worker_pdk

    def report(cmp_block):
        return {
            "environment": {
                "remote": {
                    "worker_environment": {},
                    "corner_section_libs_comparison": cmp_block,
                }
            }
        }

    _print_worker_pdk(report({"status": "match"}))
    assert capsys.readouterr().out.splitlines() == ["corner_section_libs: match"]

    _print_worker_pdk(
        report({"status": "not_comparable", "reason": "runner_libs_unavailable"})
    )
    assert capsys.readouterr().out.splitlines() == [
        "corner_section_libs: not_comparable (runner_libs_unavailable)"
    ]

    _print_worker_pdk(
        report({"status": "mismatch", "warning": "differs", "names": ["res.lib"]})
    )
    assert capsys.readouterr().out.splitlines() == [
        "corner_section_libs: mismatch",
        "warning: differs",
    ]

    for absent in (None, "x", {}, {"status": ""}):
        _print_worker_pdk(report(absent))
        assert "corner_section_libs" not in capsys.readouterr().out


def test_print_worker_pdk_prints_both_identity_lines_in_order(capsys):
    """The `models_lib:` line #5321 added keeps its place; the new line sits
    next to it, so a per-section run shows why the first says nothing."""
    from klayout_tools.cli.sim_pdk_text import _print_worker_pdk

    _print_worker_pdk(
        {
            "environment": {
                "remote": {
                    "worker_environment": {"pdk": {"models_staged": False}},
                    "models_lib_comparison": {
                        "status": "not_comparable",
                        "reason": "no_models_lib",
                    },
                    "corner_section_libs_comparison": {"status": "match"},
                }
            }
        }
    )
    assert capsys.readouterr().out.splitlines() == [
        "models_lib: not_comparable (no_models_lib)",
        "corner_section_libs: match",
        "models_staged: false",
    ]


def test_apply_engine_host_wires_both_comparisons_end_to_end():
    """Guard the wiring, not just the helper: `sim.apply_engine_host` is what
    production calls, and it must see an `environment` that already carries
    `models_lib` and `corner_section_libs` (sim.py builds both before calling
    it). A future reordering that moved the call earlier would silently make
    every client list unavailable."""
    from klayout_tools import sim

    environment = {
        "models_lib": {"path": None, "scope": "absent"},
        "models_lib_sha256": None,
        "models_lib_sha256_scope": "client",
        "corner_section_libs": [
            _entry("nfet.lib", _SHA_A),
            _entry("res.lib", _SHA_B),
        ],
        "remote": {
            "worker_environment": {"pdk": {"models_staged": False}},
            "runner_models_lib_sha256": None,
            "runner_corner_section_libs": [{"name": "nfet.lib", "sha256": _SHA_A}],
        },
    }
    sim.apply_engine_host(environment, backend="batch", client_ngspice_binary=None)
    remote = environment["remote"]
    assert remote["models_lib_comparison"] == {
        "status": "not_comparable",
        "reason": "no_models_lib",
    }
    assert remote["corner_section_libs_comparison"] == {"status": "match"}


def test_apply_engine_host_leaves_local_runs_untouched():
    """A local run has no runner to compare against, so neither key appears."""
    from klayout_tools import sim

    environment = {
        "models_lib": {"path": None, "scope": "absent"},
        "corner_section_libs": [_entry("nfet.lib", _SHA_A)],
    }
    sim.apply_engine_host(environment, backend="local", client_ngspice_binary=None)
    assert "corner_section_libs_comparison" not in environment
    assert "models_lib_comparison" not in environment
