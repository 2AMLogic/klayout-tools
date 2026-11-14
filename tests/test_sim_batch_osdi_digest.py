"""Issue #5562: a runner-resolved `osdi_preload` entry carries the runner's
own digest, so an off-host report can say which OSDI bytes were loaded.

`environment.osdi_preload_runner_resolved[]` named the entry and why the
runner owned it and stopped there, while the digest already existed one level
down: the runner runs a full `--backend local-parallel` klt sweep, whose
on-host settling emits `environment.osdi_preload` as `{name, path, scope,
sha256}` per file. These tests cover the lift of that inner array to
`environment.remote.runner_osdi_preload` and the join of its digests onto the
outer array by *declared index*.

The index is the join key, never the entry string: a declared list may repeat
an entry, which makes a string match ambiguous where the index never is. And
an unobtainable digest is `null` -- never the submitting host's own hash of a
file the runner never read, which is the substitution this issue exists to
prevent.
"""

from klayout_tools import sim_batch_features as sbf

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _inner(entries):
    """A collected inner report whose `environment.osdi_preload` is `entries`."""
    return {"environment": {"osdi_preload": entries}}


def _entry(name, sha256):
    """One `_model_file_entries` entry as the runner's own report writes it."""
    return {"name": name, "path": None, "scope": "external", "sha256": sha256}


# --- the lift: environment.remote.runner_osdi_preload ------------------------


def test_runner_osdi_preload_lifts_name_and_sha256_in_declared_order():
    lifted = sbf.runner_osdi_preload(
        _inner([_entry("a.osdi", _SHA_A.upper()), _entry("b.osdi", _SHA_B)])
    )
    assert lifted == [
        {"name": "a.osdi", "sha256": _SHA_A},
        {"name": "b.osdi", "sha256": _SHA_B},
    ]


def test_runner_osdi_preload_is_null_when_the_runner_does_not_report_it():
    """`None`, not `[]`: "older runner / unreported" stays distinguishable
    from "the run declared nothing"."""
    lift = sbf.runner_osdi_preload
    assert lift({"environment": {}}) is None
    assert lift({"environment": "not-a-dict"}) is None
    assert lift(_inner("not-a-list")) is None
    assert lift({}) is None
    assert lift(None) is None


def test_runner_osdi_preload_keeps_the_slot_of_a_malformed_entry():
    """A malformed entry's value is not propagated -- but its *slot* is kept.
    Dropping it would shift every later index and attribute one entry's bytes
    to another, which is worse than reporting the digest as unknown."""
    lifted = sbf.runner_osdi_preload(
        _inner(
            [
                _entry("a.osdi", "not-a-sha256"),
                "not-a-dict",
                {"sha256": _SHA_B},
                _entry("d.osdi", _SHA_A),
            ]
        )
    )
    assert lifted == [
        {"name": "a.osdi", "sha256": None},
        {"name": None, "sha256": None},
        {"name": None, "sha256": _SHA_B},
        {"name": "d.osdi", "sha256": _SHA_A},
    ]


def test_collected_report_env_surfaces_the_lift_beside_the_models_lib_hash():
    env = sbf.collected_report_env({}, _inner([_entry("a.osdi", _SHA_A)]))
    assert env["runner_osdi_preload"] == [{"name": "a.osdi", "sha256": _SHA_A}]
    assert "runner_models_lib_sha256" in env
    # A request declaring no `osdi_preload` gains exactly one new key, `null`.
    assert (
        sbf.collected_report_env({}, {"environment": {}})["runner_osdi_preload"] is None
    )


# --- the join: environment.osdi_preload_runner_resolved[].sha256 -------------


def _resolved(entry, index, reason="env_var"):
    """One outer entry as `osdi_runner_environment_fields` emits it."""
    return {"entry": entry, "index": index, "reason": reason, "sha256": None}


def _join(resolved, remote, backend="batch"):
    sbf.attach_runner_osdi_digests(
        {"osdi_preload_runner_resolved": resolved, "remote": remote}, backend
    )
    return [entry["sha256"] for entry in resolved]


def test_join_takes_the_digest_at_the_entry_s_own_declared_index():
    """Index 1 of a mixed list: index 0 was host-local (staged), so the
    runner's array and the outer array do not line up positionally."""
    lifted = [_entry("local.osdi", _SHA_A), _entry("baked.osdi", _SHA_B)]
    assert _join(
        [_resolved("$PDK_ROOT/osdi/baked.osdi", 1)],
        {"runner_osdi_preload": sbf.runner_osdi_preload(_inner(lifted))},
    ) == [_SHA_B]


def test_join_is_by_index_never_by_entry_string():
    """A declared list may repeat an entry; each occurrence gets its own
    slot's digest."""
    entry = "$PDK_ROOT/osdi/a.osdi"
    lifted = [
        {"name": "a.osdi", "sha256": _SHA_A},
        {"name": "a.osdi", "sha256": _SHA_B},
    ]
    assert _join(
        [_resolved(entry, 0), _resolved(entry, 1)], {"runner_osdi_preload": lifted}
    ) == [_SHA_A, _SHA_B]


def test_join_reports_null_rather_than_guessing_a_digest():
    one = [{"name": "a.osdi", "sha256": _SHA_A}]
    # An older runner that never reported `environment.osdi_preload`.
    assert _join([_resolved("$PDK_ROOT/a.osdi", 0)], {"runner_osdi_preload": None}) == [
        None
    ]
    # No `environment.remote` at all.
    assert _join([_resolved("$PDK_ROOT/a.osdi", 0)], None) == [None]
    # An index the runner's array does not reach.
    assert _join([_resolved("$PDK_ROOT/a.osdi", 5)], {"runner_osdi_preload": one}) == [
        None
    ]
    # The runner reported that slot with no usable digest.
    assert _join(
        [_resolved("$PDK_ROOT/a.osdi", 0)],
        {"runner_osdi_preload": [{"name": "a.osdi", "sha256": None}]},
    ) == [None]


def test_join_refuses_a_digest_whose_file_name_disagrees():
    """Corroborate index with the file name before trusting a digest: a
    runner whose array does not line up with the declared list must yield
    "unknown", never another entry's bytes."""
    assert _join(
        [_resolved("$PDK_ROOT/osdi/a.osdi", 0)],
        {"runner_osdi_preload": [{"name": "somethingelse.osdi", "sha256": _SHA_A}]},
    ) == [None]


def test_join_uses_fleet_consensus_and_nulls_on_disagreement():
    """A sharded run reports per member. Every shard ran the same request, so
    they must agree; a disagreeing or lost shard is unknown, not a vote."""
    agree = [{"name": "a.osdi", "sha256": _SHA_A}]
    disagree = [{"name": "a.osdi", "sha256": _SHA_B}]

    def run(members):
        return _join([_resolved("$PDK_ROOT/osdi/a.osdi", 0)], {"fleet": members})

    assert run([{"runner_osdi_preload": agree}] * 2) == [_SHA_A]
    assert run([{"runner_osdi_preload": agree}, {"runner_osdi_preload": disagree}]) == [
        None
    ]
    assert run([{"runner_osdi_preload": agree}, {"runner_osdi_preload": None}]) == [
        None
    ]
    assert run([{"runner_osdi_preload": agree}, None]) == [None]
    assert run([]) == [None]


def test_join_is_a_no_op_off_the_batch_backend():
    """#5321 set the batch-only precedent for this field family; the SSH
    `remote` backend keeps the explicit `null` it was given."""
    lifted = {"runner_osdi_preload": [{"name": "a.osdi", "sha256": _SHA_A}]}
    for backend in ("remote", "local", "local-parallel"):
        assert _join([_resolved("$PDK_ROOT/osdi/a.osdi", 0)], lifted, backend) == [None]


def test_join_leaves_an_undeclared_request_untouched():
    environment = {"remote": {"runner_osdi_preload": None}}
    sbf.attach_runner_osdi_digests(environment, "batch")
    assert environment == {"remote": {"runner_osdi_preload": None}}


def test_finalize_batch_remote_performs_the_join():
    """`sim.apply_engine_host` calls one finaliser, so the join must come out
    of it beside the models_lib comparison (#5321) and roll-ups (#5522)."""
    resolved = [_resolved("$PDK_ROOT/osdi/a.osdi", 0)]
    environment = {
        "osdi_preload_runner_resolved": resolved,
        "remote": {"runner_osdi_preload": [{"name": "a.osdi", "sha256": _SHA_A}]},
    }

    sbf.finalize_batch_remote(environment, "batch")

    assert resolved[0]["sha256"] == _SHA_A
    assert environment["remote"]["models_lib_comparison"]["status"] == "not_comparable"
