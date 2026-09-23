"""Contract tests for the ratified functional-verification run-identity v1
pilot (issue #2097, Candidate A; operator ratification 2026-09-22).

These tests exercise the *contract* -- the reference checker in
``tests/helpers/identity_contract_v1.py`` plus the fixture tree in
``tests/fixtures/functional_verification_identity_v1/`` -- not a producer.
The production runner is deliberately untouched this increment: the issue
forbids documenting or shipping a producer that does not exist yet, and
forbids any production dependency collector or signoff-policy change.

Every test needs no cocotb and no simulator (the synthetic check in
``design/tb/tb_counter.py`` is plain CPython), so the whole file runs in
the plain ``--extra dev`` environment. Each test maps to an item of the
issue's acceptance checklist:

- item 3 (checker + fixtures + one command): ``test_golden_vectors``,
  ``test_one_command_invocation``
- item 4 (repeat / key order / unrelated file):
  ``test_positive_repeat_and_unrelated_file_control``,
  ``test_golden_vectors`` (reordered vector)
- item 5 (independent mutation matrix):
  ``test_input_mutation_changes_identity``
- item 6 (relocation vs rename vs ordering vs embedded paths):
  ``test_relocation_preserves_identity``,
  ``test_logical_rename_changes_identity``,
  ``test_embedded_absolute_path_relocation_changes_identity``,
  ``test_generated_shim_absolute_path_relocation_changes_identity``;
  ordering rows live in the mutation matrix
- item 7 (rejections): ``test_reject_*``
- item 8 (output integrity separate): ``test_output_corruption_fails_``,
  ``test_report_reformat_affects_only_external_pin``
- item 9 (synthetic Python boundary check):
  ``test_synthetic_python_check_boundary``
- item 10 (undeclared dynamic input control):
  ``test_undeclared_dynamic_input_control``
- item 11 (consumer decision table): ``test_consumer_decision_table``
- item 12 (no cross-comparison with layout hashes; disclosure of
  unknown/dirty tool identity): ``test_no_cross_comparison_with_layout_hash``,
  ``test_unknown_dirty_tool_identity_disclosed``

Pre/post input reads (issue #2097: "an observed change refuses a bound
result", and the change-and-restore caveat):
``test_snapshot_detects_observed_mutation``,
``test_snapshot_change_and_restore_not_detected``.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers.identity_contract_v1 import (
    BASELINE_CLOSURE_REASONS,
    ContractError,
    InputMutationError,
    build_input_manifest,
    build_run_identity,
    canonical_bytes,
    check_golden_vectors,
    input_sha256,
    parse_manifest_bytes,
    sha256_bytes,
    snapshot_declared_bytes,
    validate_input_manifest,
    validate_run_identity,
    verify_citation,
    verify_snapshot_unchanged,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "functional_verification_identity_v1"
CHECKER_SCRIPT = REPO_ROOT / "tests" / "helpers" / "identity_contract_v1.py"

GOLDEN_CANONICAL_SHA256 = (
    "c8adf49a845034dda1f9982a33a844d630e5c67ea4083f8cb5a41395aae985cb"
)
GOLDEN_INPUT_SHA256 = "a408dd6ff75a61add23e40c1f793cc6c9c6eb277a38cf1e7ef00e82c7c60299a"


# --------------------------------------------------------------------------- #
# Shared builders (mirrors of the intended producer's resolution outputs)
# --------------------------------------------------------------------------- #


def _declarations():
    """The caller's explicit ``options.evidence.files`` inventory: include
    bytes come from this inventory only -- include directories are never
    recursively hashed (issue #2097)."""
    return [
        {
            "root": "design",
            "path": "rtl/include/counter_defs.svh",
            "roles": "rtl_include",
        },
        {
            "root": "design",
            "path": "tb/expected_counts.py",
            "roles": "testbench_helper",
        },
        {"root": "design", "path": "tb/fixtures/vectors.json", "roles": "fixture"},
    ]


def _automatic():
    """Producer-resolved automatic inputs: ordered RTL sources, the
    resolved entry testbench, original SDF, generated build inputs."""
    return [
        {"root": "design", "path": "rtl/counter.sv", "roles": "rtl_source"},
        {"root": "design", "path": "tb/tb_counter.py", "roles": "testbench_entry"},
        {"root": "design", "path": "sdf/tb_counter_typ.sdf", "roles": "sdf_original"},
        {
            "root": "generated",
            "path": "sdf_wrapper.sv",
            "roles": "generated_source",
            "file": FIXTURES / "generated" / "sdf_wrapper.sv",
        },
    ]


def _recipe():
    return {
        "requested_top": "counter",
        "effective_top": "counter",
        "testbench_module": "tb_counter",
        "entry_testbench_logical_id": "design:tb/tb_counter.py",
        "selected_testcases": ["test_counter_counts"],
        "user_source_ids": ["design:rtl/counter.sv"],
        "generated_source_ids": ["generated:sdf_wrapper.sv"],
        "include_search_roots": ["design:rtl/include"],
        "parameters": {"WIDTH": 8},
        "defines": {"SIM_MODE": "café"},
        "requested_build_args": ["-g2005-sv"],
        "effective_build_args": ["-g2005-sv"],
        "timescale": "1ns/1ps",
        "requested_seed": 1234,
        "effective_seed": 1234,
        "coverage_enabled": False,
        "trace_enabled": False,
        "original_sdf_logical_id": "design:sdf/tb_counter_typ.sdf",
        "effective_sdf_logical_id": "generated:sdf_wrapper.sv",
        "sdf_corner": "typ",
        "sdf_annotation_config": {"annotated": True, "scale": "1.0:1.0:1.0"},
        "environment": [
            {
                "name": "KLT_FIXTURE_SEED",
                "state": "set",
                "value_sha256": sha256_bytes(b"1234"),
            },
            {"name": "KLT_FIXTURE_UNSET", "state": "unset", "value_sha256": None},
        ],
    }


def _toolchain():
    return {
        "engine_name": "icarus",
        "engine_version": "13.0 (stable)",
        "cocotb_version": "2.0.0",
        "python_implementation": "CPython",
        "python_version": "3.12.3",
        "klt_build_version": "0.2.0+gabc1234def0",
        "klt_build_identity": {
            "git_commit": "abc1234def0abc1234def0abc1234def0abc1234",
            "git_tag": None,
            "dirty": False,
            "is_release": False,
        },
    }


class _Context:
    """Per-test request context: a disposable tree plus the resolution
    outputs a producer would feed the manifest builder."""

    def __init__(self, tree):
        self.tree = tree
        self.design = tree / "design"
        self.generated_dir = tree / "generated"
        self.roots = {"design": self.design}
        self.declarations = _declarations()
        self.automatic = _automatic()
        # Generated build inputs live in THIS tree's build directory, not
        # in the committed fixture -- retarget the locator.
        self.automatic[-1]["file"] = self.generated_dir / "sdf_wrapper.sv"
        self.recipe = _recipe()
        self.toolchain = _toolchain()

    def manifest(self):
        return build_input_manifest(
            self.roots,
            self.declarations,
            self.automatic,
            self.recipe,
            self.toolchain,
        )

    def run_identity(self):
        return build_run_identity(
            self.manifest(),
            input_locators={
                "roots": {"design": str(self.design)},
                "build_dir": str(self.generated_dir),
            },
        )

    def current_files(self, manifest=None):
        """The caller's explicit current input-root mapping: logical ID ->
        physical path under THIS tree (consumer-side re-hash inputs)."""
        mapping = {}
        for entry in (manifest or self.manifest())["files"]:
            root_name, _, path_text = entry["logical_id"].partition(":")
            if root_name == "generated":
                mapping[entry["logical_id"]] = self.generated_dir / path_text
            else:
                mapping[entry["logical_id"]] = self.design / path_text
        return mapping


@pytest.fixture
def tree(tmp_path):
    """A disposable copy of the committed fixture tree."""
    dest = tmp_path / "run-a"
    shutil.copytree(FIXTURES / "design", dest / "design")
    shutil.copytree(FIXTURES / "generated", dest / "generated")
    return _Context(dest)


@pytest.fixture
def manifest(tree):
    return tree.manifest()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_synthetic_check(ctx):
    """Execute the tree's synthetic Python check (issue #2097 item 9:
    "one small synthetic Python check in disposable trees containing only
    its declared files"). Plain CPython -- no cocotb, no simulator."""
    return subprocess.run(
        [sys.executable, str(ctx.design / "tb" / "tb_counter.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )


# --------------------------------------------------------------------------- #
# Item 3/4: golden vectors, one-command invocation, repeat/key-order/
# unrelated-file controls
# --------------------------------------------------------------------------- #


def test_golden_vectors():
    """Item 3/4: the published literal golden byte/digest vectors must
    match exactly, and the equivalent object-key-order vector must hash
    identically (UTF-8 canonical JSON, sorted keys, no ASCII escaping,
    arrays in order, int/float distinct)."""
    raw_canonical = (FIXTURES / "manifests" / "manifest.v1.json").read_bytes()
    raw_reordered = (FIXTURES / "manifests" / "manifest.v1.reordered.json").read_bytes()

    # The reordered fixture must genuinely be a different byte string, or
    # the equivalence claim below would be vacuous.
    assert raw_canonical != raw_reordered

    manifest_a = parse_manifest_bytes(raw_canonical)
    manifest_b = parse_manifest_bytes(raw_reordered)
    validate_input_manifest(manifest_a)
    validate_input_manifest(manifest_b)

    assert canonical_bytes(manifest_a) == canonical_bytes(manifest_b)
    # The canonical fixture is stored in exact canonical bytes: the
    # published literal golden BYTE vector is the file itself.
    assert canonical_bytes(manifest_a) == raw_canonical
    assert sha256_bytes(raw_canonical) == GOLDEN_CANONICAL_SHA256
    assert input_sha256(manifest_a) == GOLDEN_INPUT_SHA256
    assert input_sha256(manifest_b) == GOLDEN_INPUT_SHA256


def test_golden_vector_file_agrees_with_published_literals():
    golden = json.loads(
        (FIXTURES / "manifests" / "golden_digests.json").read_text(encoding="utf-8")
    )
    vectors = {v["manifest"]: v for v in golden["vectors"]}
    assert vectors["manifest.v1.json"]["input_sha256"] == GOLDEN_INPUT_SHA256
    assert (
        vectors["manifest.v1.json"]["canonical_manifest_sha256"]
        == GOLDEN_CANONICAL_SHA256
    )
    assert vectors["manifest.v1.reordered.json"] == vectors["manifest.v1.json"] | {
        "manifest": "manifest.v1.reordered.json"
    }


def test_one_command_invocation():
    """Item 3: the published one-command invocation must exit 0."""
    assert check_golden_vectors() == []
    result = subprocess.run(
        [sys.executable, str(CHECKER_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert GOLDEN_INPUT_SHA256 in result.stdout
    assert "all golden vectors match" in result.stdout


def test_positive_repeat_and_unrelated_file_control(tree):
    """Item 4: rebuilding the same request must repeat the identical
    digest, and a new file OUTSIDE the declared inventory must leave it
    unchanged (the unrelated-file control)."""
    first = input_sha256(tree.manifest())
    second = input_sha256(tree.manifest())
    assert first == second

    _write(tree.design / "notes.txt", "undeclared scratch, not in any role\n")
    _write(tree.generated_dir / "scratch.log", "undeclared build noise\n")
    assert input_sha256(tree.manifest()) == first


# --------------------------------------------------------------------------- #
# Item 5: the independent mutation matrix
# --------------------------------------------------------------------------- #


def _append_line(path, line):
    path.write_text(path.read_text(encoding="utf-8") + line, encoding="utf-8")


#
# Each row mutates exactly one identity-bearing input; every row must move
# the digest (issue #2097 item 5).
#
MUTATIONS = {
    "rtl_source_bytes": lambda ctx: _append_line(
        ctx.design / "rtl" / "counter.sv", "// mutated\n"
    ),
    "include_header_bytes": lambda ctx: _write(
        ctx.design / "rtl" / "include" / "counter_defs.svh",
        "`define COUNTER_DEFAULT_WIDTH 16\n",
    ),
    "entry_testbench_bytes": lambda ctx: _append_line(
        ctx.design / "tb" / "tb_counter.py", "# mutated\n"
    ),
    "helper_bytes": lambda ctx: _write(
        ctx.design / "tb" / "expected_counts.py", "MULTIPLIER = 2\n"
    ),
    "fixture_bytes": lambda ctx: _write(
        ctx.design / "tb" / "fixtures" / "vectors.json",
        '{"a": 3, "b": 4, "expected_sum": 8}\n',
    ),
    "sdf_bytes": lambda ctx: _append_line(
        ctx.design / "sdf" / "tb_counter_typ.sdf", "// mutated\n"
    ),
    "generated_bytes": lambda ctx: _append_line(
        ctx.generated_dir / "sdf_wrapper.sv", "// mutated\n"
    ),
    "parameter_value": lambda ctx: ctx.recipe["parameters"].update({"WIDTH": 9}),
    "parameter_int_float_form": lambda ctx: ctx.recipe["parameters"].update(
        {"WIDTH": 8.0}
    ),
    "define_value": lambda ctx: ctx.recipe["defines"].update({"SIM_MODE": "identity"}),
    "define_added": lambda ctx: ctx.recipe["defines"].update({"EXTRA": "1"}),
    "selected_top": lambda ctx: ctx.recipe.update(
        {"requested_top": "counter_tiny", "effective_top": "counter_tiny"}
    ),
    "selected_testcase": lambda ctx: ctx.recipe["selected_testcases"].append(
        "test_counter_wrap"
    ),
    "requested_seed": lambda ctx: ctx.recipe.update({"requested_seed": 5678}),
    "effective_seed": lambda ctx: ctx.recipe.update({"effective_seed": 5678}),
    "sdf_corner": lambda ctx: ctx.recipe.update({"sdf_corner": "fast"}),
    "sdf_annotation_options": lambda ctx: ctx.recipe["sdf_annotation_config"].update(
        {"scale": "2.0:2.0:2.0"}
    ),
    "sdf_dropped_from_run": lambda ctx: ctx.recipe.update(
        {
            "original_sdf_logical_id": None,
            "effective_sdf_logical_id": None,
            "sdf_corner": None,
            "sdf_annotation_config": None,
        }
    ),
    "environment_value": lambda ctx: ctx.recipe["environment"][0].update(
        {"value_sha256": sha256_bytes(b"9999")}
    ),
    "environment_set_to_unset": lambda ctx: ctx.recipe["environment"].__setitem__(
        0, {"name": "KLT_FIXTURE_SEED", "state": "unset", "value_sha256": None}
    ),
    "tool_identity": lambda ctx: ctx.toolchain.update(
        {"klt_build_version": "0.2.0+gdef5678abc9"}
    ),
    "tool_identity_dirty": lambda ctx: ctx.toolchain["klt_build_identity"].update(
        {"dirty": True}
    ),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_input_mutation_changes_identity(tree, mutation):
    """Item 5: each relevant single mutation must change the input
    identity. File-byte rows re-hash through the builder; recipe/toolchain
    rows change the manifest directly."""
    MUTATIONS[mutation](tree)
    mutated = tree.manifest()
    baseline = build_input_manifest(
        {"design": FIXTURES / "design"},
        _declarations(),
        _automatic(),
        _recipe(),
        _toolchain(),
    )
    assert input_sha256(mutated) != input_sha256(baseline)


def _context_with_two_of_each(tmp_path, name):
    """A context whose ordered arrays each carry two entries, so order is
    observable."""
    ctx = _Context(tmp_path / name)
    shutil.copytree(FIXTURES / "design", ctx.design)
    shutil.copytree(FIXTURES / "generated", ctx.generated_dir)
    _write(ctx.design / "rtl" / "extra.sv", "// second RTL source\n")
    _write(ctx.design / "rtl" / "include" / "extra_defs.svh", "// extra include\n")
    _write(ctx.generated_dir / "sdf_split_0.sdf", "(DELAYFILE)\n")
    ctx.declarations.append(
        {"root": "design", "path": "rtl/include/extra_defs.svh", "roles": "rtl_include"}
    )
    ctx.automatic.append(
        {"root": "design", "path": "rtl/extra.sv", "roles": "rtl_source"}
    )
    ctx.automatic.append(
        {
            "root": "generated",
            "path": "sdf_split_0.sdf",
            "roles": "generated_source",
            "file": ctx.generated_dir / "sdf_split_0.sdf",
        }
    )
    ctx.recipe["user_source_ids"] = [
        "design:rtl/counter.sv",
        "design:rtl/extra.sv",
    ]
    ctx.recipe["generated_source_ids"] = [
        "generated:sdf_split_0.sdf",
        "generated:sdf_wrapper.sv",
    ]
    ctx.recipe["include_search_roots"] = [
        "design:rtl/include",
        "design:rtl/include2",
    ]
    ctx.recipe["requested_build_args"] = ["-g2005-sv", "-Wall"]
    ctx.recipe["effective_build_args"] = ["-g2005-sv", "-Wall"]
    _write(ctx.design / "rtl" / "include2" / ".keep", "")
    return ctx


def test_ordered_arrays_identify_order(tmp_path):
    """Items 5/6: swapping two entries of any ordered array changes the
    identity even though the content SET is identical -- semantically
    ordered build inputs are never sorted merely to stabilize a hash, and
    ordered execution arrays retain duplicates."""
    base = _context_with_two_of_each(tmp_path, "base")
    baseline = input_sha256(base.manifest())

    swapped = _context_with_two_of_each(tmp_path, "src-order")
    swapped.recipe["user_source_ids"].reverse()
    assert input_sha256(swapped.manifest()) != baseline

    swapped = _context_with_two_of_each(tmp_path, "inc-order")
    swapped.recipe["include_search_roots"].reverse()
    assert input_sha256(swapped.manifest()) != baseline

    swapped = _context_with_two_of_each(tmp_path, "gen-order")
    swapped.recipe["generated_source_ids"].reverse()
    assert input_sha256(swapped.manifest()) != baseline

    swapped = _context_with_two_of_each(tmp_path, "arg-order")
    swapped.recipe["requested_build_args"] = ["-Wall", "-g2005-sv"]
    swapped.recipe["effective_build_args"] = ["-Wall", "-g2005-sv"]
    assert input_sha256(swapped.manifest()) != baseline

    # Duplicates in an ordered execution array are retained, not deduped:
    # duplicating one entry is itself an identity change.
    duplicated = _context_with_two_of_each(tmp_path, "dup")
    duplicated.recipe["user_source_ids"].append("design:rtl/counter.sv")
    assert input_sha256(duplicated.manifest()) != baseline


# --------------------------------------------------------------------------- #
# Item 6: relocation vs logical rename vs embedded absolute paths
# --------------------------------------------------------------------------- #


def test_relocation_preserves_identity(tmp_path):
    """Item 6: moving identical ordinary inputs together (same logical
    structure, different physical root) keeps identity stable; only the
    non-identity locator block differs."""
    tree_a = _Context(tmp_path / "a")
    shutil.copytree(FIXTURES / "design", tree_a.design)
    shutil.copytree(FIXTURES / "generated", tree_a.generated_dir)

    tree_b = _Context(tmp_path / "deeper" / "elsewhere" / "b")
    shutil.copytree(FIXTURES / "design", tree_b.design)
    shutil.copytree(FIXTURES / "generated", tree_b.generated_dir)

    block_a = tree_a.run_identity()
    block_b = tree_b.run_identity()
    assert block_a["input_sha256"] == block_b["input_sha256"]
    # The physical locations moved and live in the non-identity locators.
    assert (
        block_a["input_locators"]["roots"]["design"]
        != block_b["input_locators"]["roots"]["design"]
    )


def test_logical_rename_changes_identity(tmp_path):
    """Item 6: renaming a logical path (same bytes) DOES change identity."""
    ctx = _Context(tmp_path / "renamed")
    shutil.copytree(FIXTURES / "design", ctx.design)
    shutil.copytree(FIXTURES / "generated", ctx.generated_dir)
    (ctx.design / "rtl" / "counter.sv").rename(
        ctx.design / "rtl" / "counter_renamed.sv"
    )
    ctx.automatic[0] = {
        "root": "design",
        "path": "rtl/counter_renamed.sv",
        "roles": "rtl_source",
    }
    ctx.recipe["user_source_ids"] = ["design:rtl/counter_renamed.sv"]

    baseline = build_input_manifest(
        {"design": FIXTURES / "design"},
        _declarations(),
        _automatic(),
        _recipe(),
        _toolchain(),
    )
    assert input_sha256(ctx.manifest()) != input_sha256(baseline)


def test_embedded_absolute_path_relocation_changes_identity(tmp_path):
    """Item 6, disclosed case: a path embedded in literal build arguments
    is still real input -- relocation legitimately changes identity there.
    The contract rewrites no strings to pretend equivalence (issue #2097:
    "test and disclose that case")."""

    def build(parent):
        ctx = _Context(parent)
        shutil.copytree(FIXTURES / "design", ctx.design)
        shutil.copytree(FIXTURES / "generated", ctx.generated_dir)
        embedded = str(ctx.design / "tb" / "tb_counter.py")
        ctx.recipe["requested_build_args"] = ["-g2005-sv", embedded]
        ctx.recipe["effective_build_args"] = ["-g2005-sv", embedded]
        return ctx

    ctx_a = build(tmp_path / "a")
    ctx_b = build(tmp_path / "different-location" / "b")
    assert ctx_a.recipe["effective_build_args"] != ctx_b.recipe["effective_build_args"]
    assert input_sha256(ctx_a.manifest()) != input_sha256(ctx_b.manifest())


def test_generated_shim_absolute_path_relocation_changes_identity(tmp_path):
    """Item 6, disclosed case: a generated SDF shim whose bytes embed an
    absolute SDF path re-hashes differently after relocation -- the
    generated bytes are real inputs, and the change is intentional."""

    def build(parent):
        ctx = _Context(parent)
        shutil.copytree(FIXTURES / "design", ctx.design)
        shutil.copytree(FIXTURES / "generated", ctx.generated_dir)
        shim = ctx.generated_dir / "sdf_wrapper_abs.sv"
        shim_path = ctx.design / "sdf" / "tb_counter_typ.sdf"
        _write(
            shim,
            "`timescale 1ns/1ps\nmodule shim;\n"
            f'    initial $sdf_annotate("{shim_path}", dut);\n'
            "endmodule\n",
        )
        ctx.automatic[-1] = {
            "root": "generated",
            "path": "sdf_wrapper_abs.sv",
            "roles": "generated_source",
            "file": shim,
        }
        ctx.recipe["generated_source_ids"] = ["generated:sdf_wrapper_abs.sv"]
        ctx.recipe["effective_sdf_logical_id"] = "generated:sdf_wrapper_abs.sv"
        return ctx

    ctx_a = build(tmp_path / "a")
    ctx_b = build(tmp_path / "moved" / "b")
    assert input_sha256(ctx_a.manifest()) != input_sha256(ctx_b.manifest())


# --------------------------------------------------------------------------- #
# Item 7: rejection table
# --------------------------------------------------------------------------- #


def _manifest_bytes_with(kind):
    """Fixture manifest bytes with one injected defect: a duplicate object
    key, an NaN literal, or a 1e400 overflow literal."""
    raw = (FIXTURES / "manifests" / "manifest.v1.json").read_bytes()
    if kind == "duplicate_key":
        return raw.replace(b'"closure"', b'"closure":null,"closure"', 1)
    if kind == "nan":
        return raw.replace(b'"WIDTH":8', b'"WIDTH":NaN')
    if kind == "overflow":
        return raw.replace(b'"WIDTH":8', b'"WIDTH":1e400')
    raise AssertionError(kind)


REJECTIONS = {
    "unsupported_schema_version_int": lambda: ("block", {"schema_version": 2}),
    "unsupported_schema_version_string": lambda: ("block", {"schema_version": "1"}),
    "duplicate_json_keys": lambda: ("raw", _manifest_bytes_with("duplicate_key")),
    "malformed_digest_uppercase": lambda: ("file_sha", "A" + "a" * 63),
    "malformed_digest_short": lambda: ("file_sha", "ab" * 31),
    "malformed_digest_nonhex": lambda: ("file_sha", "z" * 64),
    "negative_byte_length": lambda: ("file_length", -1),
    "non_finite_nan_literal": lambda: ("raw", _manifest_bytes_with("nan")),
    "non_finite_overflow_literal": lambda: ("raw", _manifest_bytes_with("overflow")),
    "non_finite_parameter_object": lambda: ("param_inf", None),
    "declared_mode_complete": lambda: ("status", "complete"),
    "unknown_closure_mode": lambda: ("mode", "observed"),
    "traversal_path": lambda: ("decl_path", "../secrets.txt"),
    "absolute_declaration_path": lambda: ("decl_path", "/etc/passwd"),
    "directory_masquerading_as_file": lambda: ("decl_path", "tb/fixtures"),
    "unknown_role": lambda: ("decl_roles", "mystery"),
    "reserved_generated_role_in_declaration": lambda: (
        "decl_roles",
        "generated_source",
    ),
    "reserved_generated_root_declaration": lambda: ("decl_root", "generated"),
    "unknown_root_in_declaration": lambda: ("decl_root", "elsewhere"),
    "missing_required_file": lambda: ("decl_path", "tb/missing_helper.py"),
    "missing_baseline_closure_reason": lambda: ("drop_reason", None),
    "input_sha256_tampered": lambda: ("recorded_digest", "f" * 64),
    "extra_manifest_section": lambda: ("extra_section", None),
    "environment_bad_state": lambda: ("env_state", "maybe"),
    "environment_duplicate_names": lambda: ("env_dup", None),
}


def _corrupt_manifest(ctx, corruptor):
    """Build a valid manifest, then corrupt one spot in place."""
    manifest = ctx.manifest()
    corruptor(manifest)
    return manifest


def _rejection_block(ctx, value):
    block = ctx.run_identity()
    block.update(value)
    return block


def _rejection_param_inf(ctx, value):
    return _corrupt_manifest(
        ctx, lambda m: m["recipe"]["parameters"].update({"GAIN": float("inf")})
    )


def _rejection_file_sha(ctx, value):
    return _corrupt_manifest(ctx, lambda m: m["files"][0].update({"sha256": value}))


def _rejection_file_length(ctx, value):
    return _corrupt_manifest(
        ctx, lambda m: m["files"][0].update({"byte_length": value})
    )


def _rejection_status(ctx, value):
    return _corrupt_manifest(ctx, lambda m: m["closure"].update({"status": value}))


def _rejection_mode(ctx, value):
    return _corrupt_manifest(ctx, lambda m: m["closure"].update({"mode": value}))


def _rejection_decl_key(key):
    def handler(ctx, value):
        ctx.declarations[-1][key] = value
        return ctx.manifest()

    return handler


def _rejection_drop_reason(ctx, value):
    def corruptor(m):
        m["closure"]["reasons"] = sorted(
            set(m["closure"]["reasons"]) - {"python_native_imports_uncollected"}
        )

    return _corrupt_manifest(ctx, corruptor)


def _rejection_recorded_digest(ctx, value):
    block = ctx.run_identity()
    block["input_sha256"] = value
    return block


def _rejection_extra_section(ctx, value):
    return _corrupt_manifest(ctx, lambda m: m.update({"extra": {}}))


def _rejection_env_state(ctx, value):
    return _corrupt_manifest(
        ctx, lambda m: m["recipe"]["environment"][0].update({"state": value})
    )


def _rejection_env_dup(ctx, value):
    def corruptor(m):
        m["recipe"]["environment"].append(copy.deepcopy(m["recipe"]["environment"][0]))

    return _corrupt_manifest(ctx, corruptor)


#: kind -> handler(ctx, value); each handler produces the broken object
#: (raising on its own for declared-record defects, which are refused at
#: build time before any bytes are hashed).
_REJECTION_HANDLERS = {
    "block": _rejection_block,
    "param_inf": _rejection_param_inf,
    "file_sha": _rejection_file_sha,
    "file_length": _rejection_file_length,
    "status": _rejection_status,
    "mode": _rejection_mode,
    "decl_path": _rejection_decl_key("path"),
    "decl_roles": _rejection_decl_key("roles"),
    "decl_root": _rejection_decl_key("root"),
    "drop_reason": _rejection_drop_reason,
    "recorded_digest": _rejection_recorded_digest,
    "extra_section": _rejection_extra_section,
    "env_state": _rejection_env_state,
    "env_dup": _rejection_env_dup,
}


def _apply_rejection(ctx, kind, value):
    handler = _REJECTION_HANDLERS.get(kind)
    if handler is None:
        raise AssertionError(f"unhandled rejection kind {kind}")
    return handler(ctx, value)


@pytest.mark.parametrize("name", sorted(REJECTIONS))
def test_reject_invalid_contract_objects(tree, name):
    """Item 7: unsupported versions, duplicate JSON keys/logical IDs,
    malformed digests, negative sizes, non-finite parameters, missing
    required files and declared-mode `complete` claims are all rejected
    before any digest is trusted."""
    kind, value = REJECTIONS[name]()
    if kind == "raw":
        # Rejection may fire at parse time (duplicate keys, NaN) or at
        # validation time (the 1e400 overflow parses to an infinite float).
        with pytest.raises(ContractError):
            parsed = parse_manifest_bytes(value)
            validate_input_manifest(parsed)
        return
    with pytest.raises(ContractError):
        # Declared-record defects raise while BUILDING the manifest
        # (unresolved/traversal/symlinked/foreign records are refused
        # before any bytes are hashed); manifest/block defects raise at
        # validation time. Both are contract rejections.
        broken = _apply_rejection(tree, kind, value)
        if isinstance(broken, dict) and "input_manifest" in broken:
            validate_run_identity(broken)
        else:
            validate_input_manifest(broken)


def test_reject_duplicate_logical_ids_conflicting_contents(tree):
    """Issue #2097: duplicate logical IDs with conflicting contents are
    rejected, not silently merged. Two generated records sharing the
    logical ID `generated:sdf_wrapper.sv` but hashing different bytes are
    the minimal conflict."""
    tree.automatic.append(
        {
            "root": "generated",
            "path": "sdf_wrapper.sv",
            "roles": "generated_source",
            "file": FIXTURES / "design" / "tb" / "expected_counts.py",
        }
    )
    with pytest.raises(ContractError, match="conflicting contents"):
        tree.manifest()


def test_reject_unreadable_declared_file(tree):
    """Item 7: an unreadable required file is refused, never hashed as
    empty."""
    victim = tree.design / "tb" / "expected_counts.py"
    victim.chmod(0o000)
    try:
        if os.access(victim, os.R_OK):  # running as root would still read it
            pytest.skip("file permissions not enforced for this user")
        with pytest.raises(ContractError, match="unreadable"):
            tree.manifest()
    finally:
        victim.chmod(0o644)


def test_reject_symlinked_declared_file(tree):
    """Issue #2097: v1 opt-in collection rejects symlinks rather than
    silently following or omitting them."""
    link = tree.design / "tb" / "linked_vectors.json"
    os.symlink(tree.design / "tb" / "fixtures" / "vectors.json", link)
    tree.declarations.append(
        {"root": "design", "path": "tb/linked_vectors.json", "roles": "fixture"}
    )
    with pytest.raises(ContractError, match="symlink"):
        tree.manifest()


def test_reject_symlinked_root(tmp_path):
    real = tmp_path / "real-design"
    shutil.copytree(FIXTURES / "design", real)
    link = tmp_path / "linked-design"
    os.symlink(real, link)
    with pytest.raises(ContractError, match="symlink"):
        build_input_manifest(
            {"design": link},
            _declarations(),
            _automatic(),
            _recipe(),
            _toolchain(),
        )


def test_reject_ambiguous_overlapping_roots(tmp_path):
    """Issue #2097: nested roots make logical assignment ambiguous."""
    base = tmp_path / "design"
    shutil.copytree(FIXTURES / "design", base)
    with pytest.raises(ContractError, match="overlapping"):
        build_input_manifest(
            {"design": base, "tb": base / "tb"},
            _declarations(),
            _automatic(),
            _recipe(),
            _toolchain(),
        )


# --------------------------------------------------------------------------- #
# Pre-/post-execution input reads
# --------------------------------------------------------------------------- #


def test_snapshot_detects_observed_mutation(tree):
    """Issue #2097: read/hash before execution, re-read after; an observed
    change refuses a bound result."""
    snapshots = snapshot_declared_bytes(tree.roots, tree.declarations, tree.automatic)
    verify_snapshot_unchanged(snapshots)
    _write(
        tree.design / "tb" / "fixtures" / "vectors.json",
        '{"a": 3, "b": 4, "expected_sum": 99}\n',
    )
    with pytest.raises(InputMutationError, match="changed"):
        verify_snapshot_unchanged(snapshots)


def test_snapshot_change_and_restore_not_detected(tree):
    """Documented limitation, issue #2097: the pre/post read "catches
    observed mutation, not change-and-restore races". The contract claims
    exactly that -- no more."""
    snapshots = snapshot_declared_bytes(tree.roots, tree.declarations, tree.automatic)
    victim = tree.design / "tb" / "fixtures" / "vectors.json"
    original = victim.read_bytes()
    victim.write_bytes(b"tampered\n")
    with pytest.raises(InputMutationError):
        verify_snapshot_unchanged(snapshots)
    victim.write_bytes(original)
    verify_snapshot_unchanged(snapshots)


# --------------------------------------------------------------------------- #
# Item 9: the synthetic Python boundary check
# --------------------------------------------------------------------------- #


def test_synthetic_python_check_boundary(tmp_path):
    """Item 9: omitting a mandatory helper makes the actual command fail;
    including it passes; mutating it changes the declared identity AND the
    command's result; an unrelated file is a control. This is a fixture
    boundary check, not a sandbox guarantee."""
    # Baseline: declared tree, command passes, identity computed.
    good = _Context(tmp_path / "good")
    shutil.copytree(FIXTURES / "design", good.design)
    shutil.copytree(FIXTURES / "generated", good.generated_dir)
    assert _run_synthetic_check(good).returncode == 0
    good_digest = input_sha256(good.manifest())

    # Omitting the mandatory helper: the declaration is unresolvable (the
    # builder refuses) AND the actual command fails with an import error.
    missing = _Context(tmp_path / "missing")
    shutil.copytree(FIXTURES / "design", missing.design)
    shutil.copytree(FIXTURES / "generated", missing.generated_dir)
    (missing.design / "tb" / "expected_counts.py").unlink()
    run = _run_synthetic_check(missing)
    assert run.returncode != 0
    assert "ModuleNotFoundError" in run.stderr
    with pytest.raises(ContractError, match="not found"):
        missing.manifest()

    # Mutating the helper changes the declared identity AND flips the
    # command's verdict.
    mutated = _Context(tmp_path / "mutated")
    shutil.copytree(FIXTURES / "design", mutated.design)
    shutil.copytree(FIXTURES / "generated", mutated.generated_dir)
    _write(mutated.design / "tb" / "expected_counts.py", "MULTIPLIER = 2\n")
    mutated_run = _run_synthetic_check(mutated)
    assert mutated_run.returncode == 1
    assert "fail" in mutated_run.stdout
    assert input_sha256(mutated.manifest()) != good_digest

    # The unrelated-file control: no identity change, no verdict change.
    control = _Context(tmp_path / "control")
    shutil.copytree(FIXTURES / "design", control.design)
    shutil.copytree(FIXTURES / "generated", control.generated_dir)
    _write(control.design / "notes.txt", "undeclared, unrelated\n")
    assert _run_synthetic_check(control).returncode == 0
    assert input_sha256(control.manifest()) == good_digest


# --------------------------------------------------------------------------- #
# Item 10: the undeclared dynamic-input negative control
# --------------------------------------------------------------------------- #


def test_undeclared_dynamic_input_control(tmp_path):
    """Item 10: the declared digest can stay identical while execution
    changes; the manifest stays `partial` and cannot satisfy a complete
    -closure requirement. This is WHY v1 closure is structurally partial."""
    passing = _Context(tmp_path / "passing")
    shutil.copytree(FIXTURES / "design", passing.design)
    shutil.copytree(FIXTURES / "generated", passing.generated_dir)
    assert _run_synthetic_check(passing).returncode == 0

    failing = _Context(tmp_path / "failing")
    shutil.copytree(FIXTURES / "design", failing.design)
    shutil.copytree(FIXTURES / "generated", failing.generated_dir)
    # runtime_control.json is read by the entry testbench but deliberately
    # absent from every declaration -- the undeclared dynamic input. It
    # lives beside the entry testbench (the TB_DIR the fixture reads).
    _write(failing.design / "tb" / "runtime_control.json", '{"mode": "always_fail"}\n')

    failing_run = _run_synthetic_check(failing)
    assert failing_run.returncode == 1
    assert input_sha256(failing.manifest()) == input_sha256(passing.manifest())

    manifest = failing.manifest()
    assert manifest["closure"]["status"] == "partial"
    assert BASELINE_CLOSURE_REASONS <= set(manifest["closure"]["reasons"])

    # The consumer policy refuses the run unless partial coverage was
    # explicitly accepted; nothing can upgrade the run to complete.
    refusenik = verify_citation(
        build_run_identity(manifest),
        expected_manifest=passing.manifest(),
        current_files=failing.current_files(manifest),
        allow_partial_inputs=False,
    )
    assert refusenik["verdict"] == "refuse"
    assert "partial_coverage_policy" in refusenik["refusals"]

    with pytest.raises(ContractError, match="cannot claim 'complete'"):
        manifest["closure"]["status"] = "complete"
        validate_input_manifest(manifest)


# --------------------------------------------------------------------------- #
# Item 8/11: output integrity + the consumer decision table
# --------------------------------------------------------------------------- #


class _Citation:
    """A fully valid synthetic citation used as the decision table's
    happy row; individual rows break exactly one independent check."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.manifest = ctx.manifest()
        self.recorded = build_run_identity(
            self.manifest,
            input_locators={"roots": {"design": str(ctx.design)}},
        )
        self.artifact_dir = ctx.tree / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = self.artifact_dir / "results.xml"
        self.trace_path = self.artifact_dir / "trace.fst"
        self.report_path = self.artifact_dir / "functional.json"
        self.results_path.write_text(
            "<testsuites><testsuite name='tb_counter'>"
            "<testcase name='test_counter_counts'/>"
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        self.trace_path.write_bytes(b"FST<<<trace bytes>>>\n")
        self.report_bytes = json.dumps(
            {"status": "pass"}, separators=(",", ":")
        ).encode()
        self.report_path.write_bytes(self.report_bytes)
        self.artifacts = [
            {
                "role": "results_xml",
                "logical_path": "results.xml",
                "byte_length": self.results_path.stat().st_size,
                "sha256": sha256_bytes(self.results_path.read_bytes()),
            },
            {
                "role": "trace",
                "logical_path": "trace.fst",
                "byte_length": self.trace_path.stat().st_size,
                "sha256": sha256_bytes(self.trace_path.read_bytes()),
            },
            {
                "role": "report",
                "logical_path": "functional.json",
                "byte_length": len(self.report_bytes),
                "sha256": sha256_bytes(self.report_bytes),
            },
        ]

    def kwargs(self, **overrides):
        kwargs = {
            "expected_manifest": self.manifest,
            "current_files": self.ctx.current_files(self.manifest),
            "artifact_inventory": copy.deepcopy(self.artifacts),
            "artifact_paths": {
                "results.xml": self.results_path,
                "trace.fst": self.trace_path,
                "functional.json": self.report_path,
            },
            "report_bytes": self.report_bytes,
            "report_sha256_pin": sha256_bytes(self.report_bytes),
            "metrics": {
                "test_count": 1,
                "passed_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
            },
            "coverage_percent": 62.5,
            "coverage_expected": True,
            "allow_partial_inputs": True,
        }
        kwargs.update(overrides)
        return kwargs


@pytest.fixture
def citation(tree):
    return _Citation(tree)


def test_valid_citation_qualifies_with_explicit_partial_acceptance(citation):
    verdict = verify_citation(citation.recorded, **citation.kwargs())
    assert verdict["verdict"] == "qualify", verdict
    assert verdict["refusals"] == []


def test_output_corruption_fails_integrity_only(citation):
    """Item 8: corrupting only output bytes fails the corresponding
    integrity check while the input identity remains unchanged. A missing
    required artifact is nonqualification, never an empty digest."""
    baseline_digest = citation.recorded["input_sha256"]
    assert input_sha256(citation.manifest) == baseline_digest

    citation.results_path.write_text("<testsuites>TAMPERED", encoding="utf-8")
    verdict = verify_citation(citation.recorded, **citation.kwargs())
    assert verdict["verdict"] == "refuse"
    assert verdict["refusals"] == ["output_integrity"]
    assert "results.xml" in verdict["checks"]["output_integrity"]["detail"]
    # The input identity is byte-for-byte unchanged by output corruption.
    assert input_sha256(citation.manifest) == baseline_digest

    citation.trace_path.write_bytes(b"TRUNCATED")
    verdict = verify_citation(citation.recorded, **citation.kwargs())
    assert "trace.fst" in verdict["checks"]["output_integrity"]["detail"]

    citation.trace_path.unlink()
    verdict = verify_citation(citation.recorded, **citation.kwargs())
    assert "trace.fst" in verdict["checks"]["output_integrity"]["detail"]
    assert "missing or unreadable" in verdict["checks"]["output_integrity"]["detail"]


def test_artifact_inventory_rejects_empty_digest(citation):
    """Issue #2097: "Missing or unreadable required artifacts are
    nonqualification, never an empty digest" -- an inventory entry with an
    empty/malformed digest fails outright."""
    kwargs = citation.kwargs()
    kwargs["artifact_inventory"][0]["sha256"] = ""
    verdict = verify_citation(citation.recorded, **kwargs)
    assert verdict["verdict"] == "refuse"
    assert "output_integrity" in verdict["refusals"]


def test_report_reformat_affects_only_external_pin(citation):
    """Issue #2097: the report's own byte hash is never inside the report;
    pretty-print changes may alter the report-BYTE pin while leaving the
    input identity unchanged."""
    original_pin = sha256_bytes(citation.report_bytes)
    reformatted = json.dumps({"status": "pass"}, indent=2).encode()

    verdict = verify_citation(
        citation.recorded, **citation.kwargs(report_bytes=reformatted)
    )
    assert verdict["verdict"] == "refuse"
    assert verdict["refusals"] == ["output_integrity"]
    assert "byte pin" in verdict["checks"]["output_integrity"]["detail"]
    assert input_sha256(citation.manifest) == citation.recorded["input_sha256"]

    # With the pin recomputed over the new bytes, only the pin moved.
    verdict = verify_citation(
        citation.recorded,
        **citation.kwargs(
            report_bytes=reformatted, report_sha256_pin=sha256_bytes(reformatted)
        ),
    )
    assert verdict["verdict"] == "qualify"
    assert original_pin != sha256_bytes(reformatted)


DECISION_TABLE = {
    # row -> (kwargs overrides, expected refusal set)
    "shape_bad_version": (
        {"recorded_override": {"schema_version": 2}},
        {
            "shape_valid",
            "manifest_matches_intended",
            "declared_bytes_current",
            "partial_coverage_policy",
        },
    ),
    "metrics_domain": (
        {
            "metrics": {
                "test_count": 1,
                "passed_count": 2,
                "failed_count": 0,
                "skipped_count": 0,
            }
        },
        {"metrics_domain"},
    ),
    "metrics_negative": (
        {
            "metrics": {
                "test_count": 1,
                "passed_count": -1,
                "failed_count": 2,
                "skipped_count": 0,
            }
        },
        {"metrics_domain"},
    ),
    "coverage_zero": (
        {"coverage_percent": 0.0},
        {"coverage_nonzero"},
    ),
    "intended_manifest_mismatch": (
        "expected_manifest_override",
        {"manifest_matches_intended"},
    ),
    "declared_bytes_stale": ("stale_bytes", {"declared_bytes_current"}),
    "output_integrity": ("corrupt_results", {"output_integrity"}),
    "partial_policy_default": (
        {"allow_partial_inputs": False},
        {"partial_coverage_policy"},
    ),
    "legacy_report_without_identity": (
        {"recorded_override": None},
        {
            "shape_valid",
            "manifest_matches_intended",
            "declared_bytes_current",
            "partial_coverage_policy",
        },
    ),
}


@pytest.mark.parametrize("row", sorted(DECISION_TABLE))
def test_consumer_decision_table(citation, row):
    """Item 11: each independent check fails alone; passing any one never
    overrides a failing one; every bad row refuses overall."""
    overrides, expected_refusals = DECISION_TABLE[row]
    kwargs = citation.kwargs()
    if overrides == "expected_manifest_override":
        drifted = copy.deepcopy(citation.manifest)
        drifted["recipe"]["parameters"]["WIDTH"] = 9
        kwargs["expected_manifest"] = drifted
    elif overrides == "stale_bytes":
        citation.ctx.design.joinpath("tb", "fixtures", "vectors.json").write_text(
            '{"a": 3, "b": 4, "expected_sum": 8}\n', encoding="utf-8"
        )
    elif overrides == "corrupt_results":
        citation.results_path.write_text("TAMPERED", encoding="utf-8")
    else:
        overrides = dict(overrides)
        recorded_override = overrides.pop("recorded_override", "absent")
        kwargs.update(overrides)
        if recorded_override != "absent":
            kwargs["recorded"] = recorded_override

    recorded = kwargs.pop("recorded", citation.recorded)
    verdict = verify_citation(recorded, **kwargs)
    assert verdict["verdict"] == "refuse"
    assert set(verdict["refusals"]) == expected_refusals


# --------------------------------------------------------------------------- #
# Item 12: no cross-comparison with layout hashes; honest tool disclosure
# --------------------------------------------------------------------------- #


def test_no_cross_comparison_with_layout_hash(citation):
    """Item 12: a verification-manifest digest is never cross-compared
    with a layout/DRC hash. Demonstrated three ways: the verifier's
    surface accepts no provenance hash; the digest is domain-separated
    from raw-file hashes; and a different run's manifest (same command
    kind) satisfies nothing."""
    # 1. Structural: the consumer API has no layout/provenance parameter.
    parameters = set(inspect.signature(verify_citation).parameters)
    assert not (parameters & {"provenance", "layout_hash", "input_hashes"})

    # 2. Domain separation: the input digest can never equal a plain
    # sha256 of any underlying file, so a DRC GDS hash and a run-identity
    # digest for the same bytes can never spuriously match.
    vectors = citation.ctx.design / "tb" / "fixtures" / "vectors.json"
    raw_hash = sha256_bytes(vectors.read_bytes())
    assert citation.recorded["input_sha256"] != raw_hash

    # 3. Two unrelated verification runs with the same command kind are
    # not interchangeable: a different run's digest does not satisfy this
    # caller's intended manifest -- and no layout hash can be substituted
    # for either, because the API accepts none.
    other = copy.deepcopy(citation.manifest)
    other["recipe"]["parameters"]["WIDTH"] = 9
    other_block = build_run_identity(other)
    assert other_block["input_sha256"] != citation.recorded["input_sha256"]
    assert other_block["input_sha256"] != raw_hash
    verdict = verify_citation(other_block, **citation.kwargs())
    assert verdict["refusals"] == ["manifest_matches_intended"]


def test_unknown_dirty_tool_identity_disclosed(tree):
    """Item 12: unknown/dirty KLT identity is DISCLOSED (closure reasons,
    null fields) rather than fabricated -- and the literal string
    "unknown" is not a valid stand-in for null."""
    ctx = tree
    ctx.recipe["effective_seed"] = None
    ctx.toolchain["klt_build_version"] = None
    ctx.toolchain["klt_build_identity"] = None
    manifest = ctx.manifest()
    assert {
        "effective_seed_unobserved",
        "klt_tool_identity_unknown",
        "python_native_imports_uncollected",
        "ambient_environment_not_isolated",
        "tool_binary_bytes_not_bound",
    } <= set(manifest["closure"]["reasons"])
    assert manifest["toolchain"]["klt_build_version"] is None
    validate_input_manifest(manifest)

    block = build_run_identity(manifest)
    verdict = verify_citation(
        block,
        expected_manifest=manifest,
        current_files=ctx.current_files(manifest),
        allow_partial_inputs=True,
    )
    assert verdict["toolchain_disclosures"] == [
        "effective_seed_unobserved",
        "klt_tool_identity_unknown",
    ]
    assert verdict["verdict"] == "qualify"  # disclosed, not fatal, when accepted

    ctx.toolchain["klt_build_version"] = "0.2.0+gabc1234def0"
    ctx.toolchain["klt_build_identity"] = {
        "git_commit": "abc1234def0abc1234def0abc1234def0abc1234",
        "git_tag": None,
        "dirty": True,
        "is_release": False,
    }
    manifest = ctx.manifest()
    assert "klt_build_dirty" in manifest["closure"]["reasons"]
    assert "klt_tool_identity_unknown" not in manifest["closure"]["reasons"]

    # Fabricating a tool identity string instead of recording null is a
    # contract violation, not a disclosure.
    ctx.toolchain["klt_build_version"] = "unknown"
    with pytest.raises(ContractError, match="record null for unknown"):
        validate_input_manifest(ctx.manifest())
