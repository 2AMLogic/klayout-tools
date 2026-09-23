"""Reference checker for the ratified functional-verification run-identity
v1 contract (issue #2097, Candidate A; operator ratification 2026-09-22).

This module is the bounded, stdlib-only reference implementation of the
*contract* -- canonicalization, hashing, manifest validation, inventory
building, pre/post input reads and the synthetic consumer verifier. It is
deliberately **not** wired into the production runner
(``klayout_tools.functional_verification``): the first increment ships
contract + fixtures + tests only, and the issue explicitly forbids
documenting or shipping a producer that does not exist yet. The producer
and consumer follow-ups draft their integration points from this module
(see ``docs/design/functional-verification-run-identity-v1.md``).

Hard boundaries encoded here (issue #2097, ratified):

- Closure is structurally ``partial`` for ``mode: "declared"``; a manifest
  claiming ``complete`` is rejected, never scored.
- No dependency collection of any kind: nothing here reads Python imports,
  traces filesystem access, or inventories tool binaries. Only explicitly
  declared files plus the caller-supplied automatic input records are
  inventoried.
- The input digest is domain-separated from every other hash role (layout,
  netlist, report bytes) by the ``klt-functional-verification-input-v1``
  prefix; the consumer verifier structurally accepts no layout hash.

One-command invocation (also published in the design doc)::

    uv run --frozen --extra dev python tests/helpers/identity_contract_v1.py

which validates the bundled golden vectors in
``tests/fixtures/functional_verification_identity_v1/`` and prints the
literal golden digests, exiting nonzero on any mismatch. The full test
suite is one command too::

    uv run --frozen --extra dev pytest \
      tests/test_functional_verification_identity_contract.py -q
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# Contract constants (issue #2097, "Report and hash semantics")
# --------------------------------------------------------------------------- #

#: Domain separator for the input digest: UTF-8 prefix + one NUL byte, then
#: the canonical manifest bytes. The version suffix makes the canonical
#: profile explicitly versioned; a future v2 changes this constant and the
#: schema version together.
DOMAIN_PREFIX = b"klt-functional-verification-input-v1\x00"

#: The only ``run_identity``/manifest schema version this checker accepts.
SCHEMA_VERSION = 1

#: Roles a caller may declare in ``options.evidence.files[].role``.
DECLARED_ROLES = frozenset(
    {"rtl_include", "testbench_helper", "fixture", "configuration"}
)

#: Roles the producer assigns automatically (resolved RTL sources, the
#: resolved entry testbench, original SDF, generated build inputs).
AUTOMATIC_ROLES = frozenset(
    {"rtl_source", "testbench_entry", "sdf_original", "generated_source"}
)

KNOWN_ROLES = DECLARED_ROLES | AUTOMATIC_ROLES

#: Root name reserved for KLT-created inputs; caller declarations may never
#: use it (issue #2097: "Reserve ``generated`` for KLT-created inputs").
RESERVED_ROOTS = frozenset({"generated"})

#: The three closure reasons every v1 declared manifest must carry, per the
#: ratified body: "At minimum name uncollected Python/native imports,
#: unisolated ambient environment/external reads and tool-binary bytes not
#: bound by version strings."
BASELINE_CLOSURE_REASONS = frozenset(
    {
        "python_native_imports_uncollected",
        "ambient_environment_not_isolated",
        "tool_binary_bytes_not_bound",
    }
)

REASON_EFFECTIVE_SEED_UNOBSERVED = "effective_seed_unobserved"
REASON_KLT_TOOL_IDENTITY_UNKNOWN = "klt_tool_identity_unknown"
REASON_KLT_BUILD_DIRTY = "klt_build_dirty"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_RECIPE_KEYS = frozenset(
    {
        "requested_top",
        "effective_top",
        "testbench_module",
        "entry_testbench_logical_id",
        "selected_testcases",
        "user_source_ids",
        "generated_source_ids",
        "include_search_roots",
        "parameters",
        "defines",
        "requested_build_args",
        "effective_build_args",
        "timescale",
        "requested_seed",
        "effective_seed",
        "coverage_enabled",
        "trace_enabled",
        "original_sdf_logical_id",
        "effective_sdf_logical_id",
        "sdf_corner",
        "sdf_annotation_config",
        "environment",
    }
)

_TOOLCHAIN_KEYS = frozenset(
    {
        "engine_name",
        "engine_version",
        "cocotb_version",
        "python_implementation",
        "python_version",
        "klt_build_version",
        "klt_build_identity",
    }
)

_BUILD_IDENTITY_KEYS = frozenset({"git_commit", "git_tag", "dirty", "is_release"})


class ContractError(ValueError):
    """A manifest, request or record violates the ratified v1 contract."""


class InputMutationError(ContractError):
    """A declared input's bytes changed between the pre- and post-run
    reads; the bound result is refused (issue #2097: "Read/hash input
    bytes before execution and re-read afterward; an observed change
    refuses a bound result")."""


# --------------------------------------------------------------------------- #
# Canonical JSON profile and hashing
# --------------------------------------------------------------------------- #


def _reject_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ContractError(f"duplicate JSON key: {key!r}")
        seen[key] = value
    return seen


def _reject_constant(name):
    raise ContractError(f"non-finite JSON number: {name}")


def parse_manifest_bytes(raw):
    """Parse manifest bytes strictly: UTF-8, JSON, no duplicate object
    keys, no NaN/Infinity literals (issue #2097: "Reject duplicate JSON
    keys and malformed/non-finite values before hashing")."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"manifest is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ContractError(f"manifest is not valid JSON: {exc}") from exc


def canonical_bytes(obj):
    """The versioned canonical profile: UTF-8 JSON, sorted object keys, no
    insignificant whitespace, no ASCII escaping, finite numbers only.
    Arrays retain order; strings receive no Unicode normalization;
    integer/float JSON forms stay distinct (``1`` vs ``1.0``)."""
    try:
        text = json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (ValueError, TypeError) as exc:
        raise ContractError(f"value is not canonicalizable JSON: {exc}") from exc
    return text.encode("utf-8")


def sha256_bytes(data):
    """Raw-byte SHA-256 as lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def is_digest(value):
    """True iff *value* is a well-formed lowercase 64-hex SHA-256 digest."""
    return isinstance(value, str) and _SHA256_RE.match(value) is not None


def input_sha256(manifest):
    """sha256(DOMAIN_PREFIX + NUL + canonical manifest bytes)."""
    return sha256_bytes(DOMAIN_PREFIX + canonical_bytes(manifest))


def sha256_file(path):
    """Streamed raw-byte hash; returns ``(byte_length, sha256_hex)``."""
    digest = hashlib.sha256()
    length = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                break
            length += len(chunk)
            digest.update(chunk)
    return length, digest.hexdigest()


# --------------------------------------------------------------------------- #
# Roots and file records
# --------------------------------------------------------------------------- #


def _reject_symlink_components(path, what):
    """Reject any symlinked component; v1 opt-in collection refuses
    symlinks rather than silently following or omitting them."""
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ContractError(
                f"{what}: symlink components are not supported in v1 "
                f"collection: {parent}"
            )


def _resolve_root(name, location):
    """Validate and resolve ONE named root; see :func:`normalize_roots`."""
    if not isinstance(name, str) or not name:
        raise ContractError("root names must be non-empty strings")
    if name in RESERVED_ROOTS:
        raise ContractError(f"root name {name!r} is reserved for KLT-created inputs")
    if not isinstance(location, (str, Path)):
        raise ContractError(f"root {name!r}: location must be a path string")
    path = Path(location)
    if not path.is_absolute():
        path = Path.cwd() / path
    _reject_symlink_components(path, f"root {name!r}")
    if not path.exists():
        raise ContractError(f"root {name!r}: no such directory: {path}")
    if not path.is_dir():
        raise ContractError(f"root {name!r}: not a directory: {path}")
    return Path(path.resolve())


def _check_root_overlaps(resolved):
    """Reject ambiguous overlapping (nested) roots: a file under both would
    have two competing logical IDs (issue #2097)."""
    names = sorted(resolved)
    for i, name in enumerate(names):
        for other in names[i + 1 :]:
            a, b = resolved[name], resolved[other]
            if a == b or a in b.parents or b in a.parents:
                raise ContractError(
                    f"ambiguous overlapping roots {name!r} and {other!r}: {a} vs {b}"
                )


def normalize_roots(roots):
    """Validate named root locators and resolve them to absolute paths.

    Rejects: the reserved ``generated`` name, empty names, nonexistent or
    non-directory roots, symlinked components, and ambiguous overlapping
    (nested) roots -- issue #2097: "Reject ambiguous overlapping roots,
    ... and symlinks for v1 opt-in collection".
    """
    if not isinstance(roots, dict) or not roots:
        raise ContractError("roots must be a non-empty object of name -> path")
    resolved = {name: _resolve_root(name, loc) for name, loc in roots.items()}
    _check_root_overlaps(resolved)
    return resolved


def _check_relative_path(path_text, what):
    """Validate a declared relative POSIX path: no absolute form, no empty
    or ``.``/``..`` segments (traversal), POSIX separators only."""
    if not isinstance(path_text, str) or not path_text:
        raise ContractError(f"{what}: path must be a non-empty string")
    if "\\" in path_text:
        raise ContractError(f"{what}: path must use POSIX separators: {path_text!r}")
    pure = PurePosixPath(path_text)
    if pure.is_absolute():
        raise ContractError(f"{what}: path must be relative to its root: {path_text!r}")
    for segment in pure.parts:
        if segment in ("", ".", ".."):
            raise ContractError(f"{what}: path traversal is rejected: {path_text!r}")
    return pure


def _hash_record_file(physical, what):
    if not physical.exists():
        raise ContractError(f"{what}: required file not found: {physical}")
    if physical.is_dir():
        raise ContractError(f"{what}: directory masquerading as a file: {physical}")
    if not physical.is_file():
        raise ContractError(f"{what}: not a regular file: {physical}")
    _reject_symlink_components(physical, what)
    try:
        return sha256_file(physical)
    except OSError as exc:
        raise ContractError(f"{what}: required file unreadable: {exc}") from exc


def _normalize_roles(roles, what, allowed):
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list) or not roles:
        raise ContractError(f"{what}: roles must be a non-empty array")
    for role in roles:
        if not isinstance(role, str) or role not in allowed:
            # For caller declarations the allowed set is DECLARED_ROLES, so
            # this also rejects the producer-reserved automatic roles
            # ("Reserve `generated` for KLT-created inputs", issue #2097).
            raise ContractError(f"{what}: unknown or reserved role: {role!r}")
    return sorted(set(roles))


def collect_declared_files(root_map, declarations):
    """Resolve the caller's explicit ``files`` inventory into record dicts
    with ``logical_id``, ``roles``, ``byte_length`` and ``sha256``.

    Every declared file must live under its named root -- files outside
    declared roots require another named root, and are never silently
    omitted (issue #2097)."""
    records = []
    for index, declaration in enumerate(declarations):
        what = f"files[{index}]"
        if not isinstance(declaration, dict):
            raise ContractError(f"{what}: must be an object")
        unknown = set(declaration) - {"root", "path", "roles"}
        if unknown:
            raise ContractError(f"{what}: unknown keys {sorted(unknown)}")
        root = declaration.get("root")
        if not isinstance(root, str) or not root:
            raise ContractError(f"{what}: root must be a non-empty string")
        if root in RESERVED_ROOTS:
            raise ContractError(
                f"{what}: root name {root!r} is reserved for KLT-created inputs"
            )
        if root not in root_map:
            raise ContractError(
                f"{what}: unknown root {root!r}; files outside declared roots "
                f"require another named root"
            )
        pure = _check_relative_path(declaration.get("path"), what)
        roles = _normalize_roles(declaration.get("roles"), what, DECLARED_ROLES)
        physical = root_map[root].joinpath(*pure.parts)
        length, digest = _hash_record_file(physical, what)
        records.append(
            {
                "logical_id": f"{root}:{pure.as_posix()}",
                "roles": roles,
                "byte_length": length,
                "sha256": digest,
            }
        )
    return records


def collect_automatic_files(root_map, records_in):
    """Resolve producer-side automatic input records (resolved RTL sources
    in build order, the resolved entry testbench, original SDF, generated
    build inputs) into inventory records.

    Each record is ``{"root", "path", "roles"}``; ordinary records resolve
    under a declared root like declared files do. Records with root
    ``"generated"`` (reserved for KLT-created inputs) carry an explicit
    ``file`` locator instead, because generated build inputs live in the
    build directory rather than under any caller root."""
    records = []
    for index, record in enumerate(records_in):
        what = f"automatic[{index}]"
        if not isinstance(record, dict):
            raise ContractError(f"{what}: must be an object")
        allowed_keys = {"root", "path", "roles", "file"}
        unknown = set(record) - allowed_keys
        if unknown:
            raise ContractError(f"{what}: unknown keys {sorted(unknown)}")
        root = record.get("root")
        if not isinstance(root, str) or not root:
            raise ContractError(f"{what}: root must be a non-empty string")
        pure = _check_relative_path(record.get("path"), what)
        roles = _normalize_roles(record.get("roles"), what, KNOWN_ROLES)
        if root in RESERVED_ROOTS:
            physical = record.get("file")
            if not isinstance(physical, (str, Path)):
                raise ContractError(
                    f"{what}: generated record requires an explicit 'file' locator"
                )
            physical = Path(physical)
            if not physical.is_absolute():
                raise ContractError(
                    f"{what}: generated 'file' locator must be absolute"
                )
        elif root in root_map:
            physical = root_map[root].joinpath(*pure.parts)
        else:
            raise ContractError(f"{what}: unknown root {root!r}")
        length, digest = _hash_record_file(physical, what)
        records.append(
            {
                "logical_id": f"{root}:{pure.as_posix()}",
                "roles": roles,
                "byte_length": length,
                "sha256": digest,
            }
        )
    return records


def merge_inventory(*record_groups):
    """Merge record groups into the sorted ``files`` inventory.

    One file may carry several roles (sorted, deduplicated). Duplicate
    logical IDs are rejected outright when their contents conflict
    (issue #2097: "duplicate logical IDs with conflicting contents")."""
    by_id = {}
    for group in record_groups:
        for record in group:
            logical_id = record["logical_id"]
            if logical_id in by_id:
                existing = by_id[logical_id]
                same = (
                    existing["byte_length"] == record["byte_length"]
                    and existing["sha256"] == record["sha256"]
                )
                if not same:
                    raise ContractError(
                        f"duplicate logical ID with conflicting contents: {logical_id}"
                    )
                existing["roles"] = sorted(
                    set(existing["roles"]) | set(record["roles"])
                )
            else:
                by_id[logical_id] = {
                    "logical_id": logical_id,
                    "roles": list(record["roles"]),
                    "byte_length": record["byte_length"],
                    "sha256": record["sha256"],
                }
    return sorted(by_id.values(), key=lambda entry: entry["logical_id"])


# --------------------------------------------------------------------------- #
# Closure reasons
# --------------------------------------------------------------------------- #


def disclosure_reasons(toolchain, effective_seed):
    """Additional closure reasons demanded by the ratified body: missing
    seed observation and unknown/dirty KLT tool identity must be *named*,
    never papered over ("Unknown values remain null/unknown")."""
    reasons = set()
    if effective_seed is None:
        reasons.add(REASON_EFFECTIVE_SEED_UNOBSERVED)
    if isinstance(toolchain, dict):
        build_version = toolchain.get("klt_build_version")
        identity = toolchain.get("klt_build_identity")
        if (
            build_version is None
            or identity is None
            or identity.get("git_commit") is None
        ):
            reasons.add(REASON_KLT_TOOL_IDENTITY_UNKNOWN)
        if isinstance(identity, dict) and identity.get("dirty") is True:
            reasons.add(REASON_KLT_BUILD_DIRTY)
    return reasons


# --------------------------------------------------------------------------- #
# Manifest builder (also builds the caller-owned expected manifest)
# --------------------------------------------------------------------------- #


def build_input_manifest(roots, declarations, automatic, recipe, toolchain):
    """Build and validate a v1 ``input_manifest`` from a request-directory
    root map, the caller's declared file inventory, the producer's
    automatic input records, the run recipe and the toolchain block.

    Closure is always ``mode: "declared"``, ``status: "partial"``; the
    baseline reasons plus any applicable disclosure reasons are filled in
    automatically. This same function is what the consumer follow-up uses
    caller-side to generate ``expected_manifest`` for the intended
    resolved request."""
    root_map = normalize_roots(roots)
    files = merge_inventory(
        collect_declared_files(root_map, declarations),
        collect_automatic_files(root_map, automatic),
    )
    reasons = set(BASELINE_CLOSURE_REASONS)
    reasons |= disclosure_reasons(toolchain, recipe.get("effective_seed"))
    manifest = {
        "files": files,
        "recipe": recipe,
        "toolchain": toolchain,
        "closure": {
            "mode": "declared",
            "status": "partial",
            "reasons": sorted(reasons),
        },
    }
    validate_input_manifest(manifest)
    return manifest


def build_run_identity(manifest, input_locators=None):
    """Assemble the additive ``run_identity`` envelope block: the identity
    fields hashed together, plus the non-identity locator block that keeps
    physical root locations, output directories and timestamps OUT of the
    digest (issue #2097: "Physical root locations, timestamps, output
    directories and diagnostic paths live in a non-identity locator
    block")."""
    block = {
        "schema_version": SCHEMA_VERSION,
        "input_manifest": manifest,
        "input_sha256": input_sha256(manifest),
    }
    if input_locators is not None:
        block["input_locators"] = input_locators
    validate_run_identity(block)
    return block


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _reject(extra, what):
    if extra:
        raise ContractError(f"{what}: unexpected keys {sorted(extra)}")


def _check_str_or_none(value, what):
    if value is not None and (not isinstance(value, str) or not value):
        raise ContractError(f"{what}: must be a non-empty string or null")


def _check_str_list(value, what):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ContractError(f"{what}: must be an array of non-empty strings")


def _check_finite_number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{what}: must be a JSON number")
    if isinstance(value, float) and (
        value != value or value in (float("inf"), float("-inf"))
    ):
        raise ContractError(f"{what}: non-finite number")


def _check_typed_value(value, what):
    """Typed parameter/define values: strings, booleans, or finite numbers
    only (issue #2097: "typed parameters/defines", "finite JSON numbers
    only")."""
    if isinstance(value, str):
        return
    _check_finite_number(value, what)


def _check_logical_id(value, what, allow_none=False):
    if value is None:
        if allow_none:
            return
        raise ContractError(f"{what}: must be a logical ID, not null")
    _check_str_or_none(value, what)
    name, sep, path_text = value.partition(":")
    if not sep or not name:
        raise ContractError(
            f"{what}: logical ID must be '<root-name>:<relative POSIX path>'"
        )
    _check_relative_path(path_text, what)


def _validate_files(files):
    if not isinstance(files, list) or not files:
        raise ContractError("files: must be a non-empty array")
    previous = None
    for index, entry in enumerate(files):
        what = f"files[{index}]"
        if not isinstance(entry, dict):
            raise ContractError(f"{what}: must be an object")
        _reject(set(entry) - {"logical_id", "roles", "byte_length", "sha256"}, what)
        logical_id = entry.get("logical_id")
        _check_logical_id(logical_id, f"{what}.logical_id", allow_none=False)
        if previous is not None and logical_id <= previous:
            raise ContractError(
                f"files: entries must be unique and sorted by logical ID "
                f"({logical_id!r} after {previous!r})"
            )
        previous = logical_id
        roles = entry.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or not role for role in roles)
        ):
            raise ContractError(f"{what}.roles: must be a non-empty array of strings")
        if sorted(set(roles)) != roles:
            raise ContractError(f"{what}.roles: must be sorted and unique")
        unknown = [role for role in roles if role not in KNOWN_ROLES]
        if unknown:
            raise ContractError(f"{what}.roles: unknown roles {unknown}")
        length = entry.get("byte_length")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise ContractError(f"{what}.byte_length: must be a non-negative integer")
        if not is_digest(entry.get("sha256")):
            raise ContractError(f"{what}.sha256: malformed SHA-256 digest")


def _validate_environment(environment):
    if not isinstance(environment, list):
        raise ContractError("recipe.environment: must be an array")
    previous = None
    for index, entry in enumerate(environment):
        what = f"recipe.environment[{index}]"
        if not isinstance(entry, dict):
            raise ContractError(f"{what}: must be an object")
        _reject(set(entry) - {"name", "state", "value_sha256"}, what)
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ContractError(f"{what}.name: must be a non-empty string")
        if previous is not None and name <= previous:
            raise ContractError(
                f"recipe.environment: entries must be unique and sorted by name "
                f"({name!r} after {previous!r})"
            )
        previous = name
        state = entry.get("state")
        if state not in ("set", "unset"):
            raise ContractError(f"{what}.state: must be 'set' or 'unset'")
        digest = entry.get("value_sha256")
        if state == "set":
            if not is_digest(digest):
                raise ContractError(
                    f"{what}.value_sha256: set variables record a SHA-256 digest"
                )
        elif digest is not None:
            raise ContractError(f"{what}.value_sha256: unset variables record null")


def _validate_recipe_strings(recipe):
    """String-ish recipe members: optional strings, the required
    testbench module, and the required entry-testbench logical ID."""
    for key in ("requested_top", "effective_top", "timescale", "sdf_corner"):
        _check_str_or_none(recipe[key], f"recipe.{key}")
    if (
        not isinstance(recipe["testbench_module"], str)
        or not recipe["testbench_module"]
    ):
        raise ContractError("recipe.testbench_module: must be a non-empty string")
    _check_logical_id(
        recipe["entry_testbench_logical_id"], "recipe.entry_testbench_logical_id"
    )
    for key in ("original_sdf_logical_id", "effective_sdf_logical_id"):
        _check_logical_id(recipe[key], f"recipe.{key}", allow_none=True)


def _validate_recipe_arrays(recipe):
    """Ordered array members: plain string arrays, plus the logical-ID
    arrays whose members must name declared/rooted files."""
    for key in (
        "selected_testcases",
        "user_source_ids",
        "generated_source_ids",
        "include_search_roots",
        "requested_build_args",
        "effective_build_args",
    ):
        _check_str_list(recipe[key], f"recipe.{key}")
    for logical_key in (
        "user_source_ids",
        "generated_source_ids",
        "include_search_roots",
    ):
        for item in recipe[logical_key]:
            _check_logical_id(item, f"recipe.{logical_key}[]")


def _validate_recipe_typed_mappings(recipe):
    """Typed parameters/defines (finite numbers/strings/bools) and the
    canonicalizable SDF annotation configuration."""
    for key in ("parameters", "defines"):
        mapping = recipe[key]
        if not isinstance(mapping, dict):
            raise ContractError(f"recipe.{key}: must be an object")
        for name, value in mapping.items():
            if not isinstance(name, str) or not name:
                raise ContractError(f"recipe.{key}: names must be non-empty strings")
            _check_typed_value(value, f"recipe.{key}[{name!r}]")
    annotation = recipe["sdf_annotation_config"]
    if annotation is None:
        return
    if not isinstance(annotation, dict):
        raise ContractError("recipe.sdf_annotation_config: must be an object or null")
    try:
        canonical_bytes(annotation)
    except ContractError as exc:
        raise ContractError(f"recipe.sdf_annotation_config: {exc}") from exc


def _validate_recipe_scalars(recipe):
    """Seeds (integer or null) and the coverage/trace boolean switches."""
    for key in ("requested_seed", "effective_seed"):
        seed = recipe[key]
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ContractError(f"recipe.{key}: must be an integer or null")
    for key in ("coverage_enabled", "trace_enabled"):
        if not isinstance(recipe[key], bool):
            raise ContractError(f"recipe.{key}: must be a boolean")


def _validate_recipe(recipe):
    if not isinstance(recipe, dict):
        raise ContractError("recipe: must be an object")
    _reject(set(recipe) - _RECIPE_KEYS, "recipe")
    missing = _RECIPE_KEYS - set(recipe)
    if missing:
        raise ContractError(f"recipe: missing keys {sorted(missing)}")
    _validate_recipe_strings(recipe)
    _validate_recipe_arrays(recipe)
    _validate_recipe_typed_mappings(recipe)
    _validate_recipe_scalars(recipe)
    _validate_environment(recipe["environment"])


def _validate_build_identity(identity):
    """``klt_build_identity``: the #2090/#2102 resolver fields, each typed
    or null (unknown identity stays null -- never a fabricated string)."""
    if identity is None:
        return
    if not isinstance(identity, dict):
        raise ContractError("toolchain.klt_build_identity: must be an object or null")
    _reject(set(identity) - _BUILD_IDENTITY_KEYS, "toolchain.klt_build_identity")
    missing = _BUILD_IDENTITY_KEYS - set(identity)
    if missing:
        raise ContractError(
            f"toolchain.klt_build_identity: missing keys {sorted(missing)}"
        )
    for key in ("git_commit", "git_tag"):
        _check_str_or_none(identity[key], f"toolchain.klt_build_identity.{key}")
    for key in ("dirty", "is_release"):
        value = identity[key]
        if value is not None and not isinstance(value, bool):
            raise ContractError(
                f"toolchain.klt_build_identity.{key}: must be a boolean or null"
            )


def _validate_toolchain(toolchain):
    if not isinstance(toolchain, dict):
        raise ContractError("toolchain: must be an object")
    _reject(set(toolchain) - _TOOLCHAIN_KEYS, "toolchain")
    missing = _TOOLCHAIN_KEYS - set(toolchain)
    if missing:
        raise ContractError(f"toolchain: missing keys {sorted(missing)}")
    for key in sorted(_TOOLCHAIN_KEYS - {"klt_build_identity"}):
        value = toolchain[key]
        _check_str_or_none(value, f"toolchain.{key}")
        if value == "unknown":
            raise ContractError(
                f"toolchain.{key}: record null for unknown values, never the "
                f"literal string 'unknown' (issue #2097: unknown values are "
                f"disclosed, not fabricated)"
            )
    _validate_build_identity(toolchain["klt_build_identity"])


def _validate_closure(closure):
    if not isinstance(closure, dict):
        raise ContractError("closure: must be an object")
    _reject(set(closure) - {"mode", "status", "reasons"}, "closure")
    missing = {"mode", "status", "reasons"} - set(closure)
    if missing:
        raise ContractError(f"closure: missing keys {sorted(missing)}")
    if closure["mode"] != "declared":
        raise ContractError(
            f"closure.mode: v1 supports only 'declared', got {closure['mode']!r}"
        )
    status = closure["status"]
    if status == "complete":
        raise ContractError(
            "closure.status: a v1 declared manifest cannot claim 'complete' "
            "(issue #2097: the partial label is structurally unavoidable in v1)"
        )
    if status != "partial":
        raise ContractError(f"closure.status: must be 'partial', got {status!r}")
    reasons = closure["reasons"]
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in reasons
    ):
        raise ContractError("closure.reasons: must be an array of non-empty strings")
    if sorted(set(reasons)) != reasons:
        raise ContractError("closure.reasons: must be sorted and unique")
    missing_reasons = BASELINE_CLOSURE_REASONS - set(reasons)
    if missing_reasons:
        raise ContractError(
            f"closure.reasons: v1 declared manifests must name the baseline "
            f"reasons {sorted(missing_reasons)}"
        )


def validate_input_manifest(manifest):
    """Validate the four identity-bearing sections. Unknown top-level
    sections are rejected: the v1 manifest has exactly ``files``,
    ``recipe``, ``toolchain`` and ``closure``."""
    if not isinstance(manifest, dict):
        raise ContractError("input_manifest: must be an object")
    _reject(
        set(manifest) - {"files", "recipe", "toolchain", "closure"}, "input_manifest"
    )
    missing = {"files", "recipe", "toolchain", "closure"} - set(manifest)
    if missing:
        raise ContractError(f"input_manifest: missing sections {sorted(missing)}")
    _validate_files(manifest["files"])
    _validate_recipe(manifest["recipe"])
    _validate_toolchain(manifest["toolchain"])
    _validate_closure(manifest["closure"])
    # Walk once for non-finite floats anywhere (1e400 parses to inf without
    # tripping parse_constant), so rejection happens before hashing.
    try:
        canonical_bytes(manifest)
    except ContractError as exc:
        raise ContractError(f"input_manifest: {exc}") from exc


def validate_run_identity(block):
    """Validate the additive ``run_identity`` envelope block and recompute
    its ``input_sha256`` from the manifest bytes."""
    if not isinstance(block, dict):
        raise ContractError("run_identity: must be an object")
    _reject(
        set(block)
        - {"schema_version", "input_manifest", "input_sha256", "input_locators"},
        "run_identity",
    )
    missing = {"schema_version", "input_manifest", "input_sha256"} - set(block)
    if missing:
        raise ContractError(f"run_identity: missing keys {sorted(missing)}")
    version = block["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SCHEMA_VERSION
    ):
        raise ContractError(
            f"run_identity.schema_version: unsupported version {version!r}; "
            f"this checker implements version {SCHEMA_VERSION} only"
        )
    validate_input_manifest(block["input_manifest"])
    recorded = block["input_sha256"]
    if not is_digest(recorded):
        raise ContractError("run_identity.input_sha256: malformed SHA-256 digest")
    expected = input_sha256(block["input_manifest"])
    if recorded != expected:
        raise ContractError(
            f"run_identity.input_sha256: does not match the manifest bytes "
            f"(recorded {recorded}, recomputed {expected})"
        )
    locators = block.get("input_locators")
    if locators is not None and not isinstance(locators, dict):
        raise ContractError(
            "run_identity.input_locators: must be an object when present"
        )
    return expected


# --------------------------------------------------------------------------- #
# Pre-/post-execution input reads
# --------------------------------------------------------------------------- #


def snapshot_declared_bytes(root_map, declarations, automatic):
    """Read and hash every declared/automatic input before execution.

    Returns a list of ``(logical_id, physical_path, byte_length, sha256)``
    tuples; :func:`verify_snapshot_unchanged` re-reads the same paths after
    execution and refuses a bound result on any observed change. This
    catches observed mutation only -- not change-and-restore races and not
    unrecorded inputs (issue #2097 says so explicitly)."""
    snapshots = []
    for record in merge_inventory(
        collect_declared_files(root_map, declarations),
        collect_automatic_files(root_map, automatic),
    ):
        root_name, _, path_text = record["logical_id"].partition(":")
        if root_name in RESERVED_ROOTS:
            generated = next(
                entry
                for entry in automatic
                if entry.get("root") == root_name
                and PurePosixPath(entry["path"]).as_posix() == path_text
            )
            physical = Path(generated["file"])
        else:
            physical = root_map[root_name].joinpath(*PurePosixPath(path_text).parts)
        length, digest = sha256_file(physical)
        if (length, digest) != (record["byte_length"], record["sha256"]):
            raise InputMutationError(
                f"declared input changed before execution: {record['logical_id']}"
            )
        snapshots.append((record["logical_id"], physical, length, digest))
    return snapshots


def verify_snapshot_unchanged(snapshots):
    """Re-read every snapshotted input after execution; raise
    :class:`InputMutationError` if any bytes changed."""
    for logical_id, physical, length, digest in snapshots:
        if not physical.exists() or not physical.is_file():
            raise InputMutationError(
                f"declared input missing after execution: {logical_id} ({physical})"
            )
        new_length, new_digest = sha256_file(physical)
        if (new_length, new_digest) != (length, digest):
            raise InputMutationError(
                f"declared input changed during execution: {logical_id}"
            )


# --------------------------------------------------------------------------- #
# Synthetic consumer verifier (contract-level; signoff is NOT touched)
# --------------------------------------------------------------------------- #

#: Independent consumer checks. Each fails on its own; no passing check
#: overrides a failing one (issue #2097 acceptance item 11).
CONSUMER_CHECKS = (
    "shape_valid",
    "metrics_domain",
    "coverage_nonzero",
    "manifest_matches_intended",
    "declared_bytes_current",
    "output_integrity",
    "partial_coverage_policy",
)


def _artifact_entries(artifact_inventory):
    for index, entry in enumerate(artifact_inventory):
        what = f"artifact_inventory[{index}]"
        if not isinstance(entry, dict):
            raise ContractError(f"{what}: must be an object")
        _reject(set(entry) - {"role", "logical_path", "byte_length", "sha256"}, what)
        role = entry.get("role")
        if not isinstance(role, str) or not role:
            raise ContractError(f"{what}.role: must be a non-empty string")
        path_text = entry.get("logical_path")
        if not isinstance(path_text, str) or not path_text:
            raise ContractError(f"{what}.logical_path: must be a non-empty string")
        length = entry.get("byte_length")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise ContractError(f"{what}.byte_length: must be a non-negative integer")
        if not is_digest(entry.get("sha256")):
            # A missing or unreadable required artifact is a
            # nonqualification, never an empty digest (issue #2097).
            raise ContractError(f"{what}.sha256: malformed or empty digest")


def _evaluate_shape(recorded):
    """The recorded ``run_identity`` must validate and self-hash. Returns
    ``(passed, detail, manifest_or_None)``."""
    if recorded is None:
        return (
            False,
            "legacy report carries no run_identity; missing legacy identity "
            "cannot satisfy the opt-in policy",
            None,
        )
    try:
        validate_run_identity(recorded)
    except ContractError as exc:
        return False, str(exc), None
    return True, "run_identity validates and self-hashes", recorded["input_manifest"]


def _evaluate_metrics(metrics):
    """Independent #2094/#2096-style count-domain sanity; the consumer
    runs these whether or not identity matched."""
    if metrics is None:
        return True, "no metrics supplied (not applicable)"
    try:
        counts = {
            key: metrics[key]
            for key in ("test_count", "passed_count", "failed_count", "skipped_count")
        }
        for key, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{key} must be a non-negative integer")
        if (
            counts["passed_count"] + counts["failed_count"] + counts["skipped_count"]
            != counts["test_count"]
        ):
            raise ContractError("passed + failed + skipped must equal test_count")
    except (KeyError, ContractError) as exc:
        return False, str(exc)
    return True, "counts consistent and in domain"


def _evaluate_coverage(coverage_percent, coverage_expected):
    """When coverage is claimed, actual executed coverage must be a
    finite, nonzero number (#2096's nonempty-execution analogue)."""
    if not coverage_expected:
        return True, "coverage not expected (not applicable)"
    if isinstance(coverage_percent, bool) or not isinstance(
        coverage_percent, (int, float)
    ):
        return False, "coverage_percent must be a number"
    if coverage_percent != coverage_percent or coverage_percent in (
        float("inf"),
        float("-inf"),
    ):
        return False, "coverage_percent must be finite"
    if coverage_percent <= 0:
        return False, "actual test coverage must be nonzero when coverage is claimed"
    return True, "coverage measured and nonzero"


def _evaluate_intended(manifest, shape_ok, expected_manifest):
    """The recorded manifest must match the CALLER-OWNED expected manifest
    byte-for-byte; a digest copied from the report itself is not evidence
    of the intended configuration (issue #2097)."""
    if not shape_ok:
        return False, "no valid recorded manifest"
    if expected_manifest is None:
        return False, "no caller-owned expected manifest supplied"
    try:
        validate_input_manifest(expected_manifest)
    except ContractError as exc:
        return False, f"expected manifest invalid: {exc}"
    if canonical_bytes(expected_manifest) == canonical_bytes(manifest):
        return True, "recorded identity matches the intended manifest byte-for-byte"
    return False, "recorded manifest differs from the caller's expected manifest"


def _evaluate_declared_bytes(manifest, shape_ok, current_files):
    """Re-hash every declared file from the caller's CURRENT input roots
    and compare to the recorded digests."""
    if not shape_ok:
        return False, "no valid recorded manifest"
    if current_files is None:
        return False, "no current input-root mapping supplied"
    failures = []
    recorded_files = {entry["logical_id"]: entry for entry in manifest["files"]}
    for logical_id, physical in sorted(current_files.items()):
        if logical_id not in recorded_files:
            failures.append(f"{logical_id}: not in recorded manifest")
            continue
        entry = recorded_files[logical_id]
        try:
            length, digest = sha256_file(physical)
        except OSError as exc:
            failures.append(f"{logical_id}: unreadable ({exc})")
            continue
        if (length, digest) != (entry["byte_length"], entry["sha256"]):
            failures.append(f"{logical_id}: current bytes differ from recorded digest")
    if failures:
        return False, "; ".join(failures)
    return (
        True,
        f"{len(recorded_files)} declared files re-hashed from the caller's "
        f"roots all match the recorded digests",
    )


def _evaluate_artifacts(artifact_inventory, artifact_paths):
    """Bind each cited artifact to its exact bytes; a missing or
    unreadable required artifact is nonqualification, never an empty
    digest (issue #2097)."""
    failures = []
    try:
        _artifact_entries(artifact_inventory)
        for entry in artifact_inventory:
            physical = (artifact_paths or {}).get(entry["logical_path"])
            if physical is None:
                failures.append(
                    f"{entry['logical_path']}: no artifact-root mapping supplied"
                )
                continue
            try:
                length, digest = sha256_file(physical)
            except OSError:
                failures.append(
                    f"{entry['logical_path']}: required artifact missing or unreadable"
                )
                continue
            if (length, digest) != (entry["byte_length"], entry["sha256"]):
                failures.append(
                    f"{entry['logical_path']}: bytes do not match the recorded digest"
                )
    except ContractError as exc:
        failures.append(str(exc))
    return failures


def _evaluate_report_pin(report_bytes, report_sha256_pin):
    """The report-byte pin is held EXTERNALLY (never inside the report);
    verify the supplied report bytes against it."""
    if report_sha256_pin is None:
        return []
    if report_bytes is None:
        return ["report byte pin present but no report bytes supplied"]
    if sha256_bytes(report_bytes) != report_sha256_pin:
        return ["report bytes do not match the externally held byte pin"]
    return []


def _evaluate_partial_policy(manifest, shape_ok, allow_partial_inputs, disclosures):
    """Explicit partial-coverage acceptance is required; the default
    refuses qualification. A v1 manifest can never offer anything but
    partial, so accepting is always an explicit caller decision."""
    if not isinstance(allow_partial_inputs, bool):
        return False, "allow_partial_inputs must be a boolean"
    if not shape_ok:
        return False, "no valid closure section; the opt-in policy cannot be satisfied"
    status = manifest["closure"]["status"]
    if status != "partial":
        return False, f"closure status {status!r} is not a valid v1 state"
    if allow_partial_inputs is False:
        return (
            False,
            "input closure is partial and allow_partial_inputs was not "
            "explicitly accepted; qualification refused",
        )
    detail = "partial input closure explicitly accepted; disclosures: " + (
        ", ".join(disclosures) if disclosures else "none"
    )
    return True, detail


def verify_citation(
    recorded,
    *,
    expected_manifest=None,
    current_files=None,
    artifact_inventory=(),
    artifact_paths=None,
    report_bytes=None,
    report_sha256_pin=None,
    metrics=None,
    coverage_percent=None,
    coverage_expected=False,
    allow_partial_inputs=False,
):
    """Contract-level consumer verification of a functional-verification
    citation carrying an opt-in ``run_identity`` block.

    This is the pilot's synthetic stand-in for the consumer follow-up: it
    encodes the ratified *policy* (independent checks, caller-owned
    expected manifest, explicit partial-coverage acceptance) without
    touching ``signoff.py``. It structurally accepts no layout/netlist
    provenance hash: the digest domains are separate by construction.

    Returns ``{"verdict": "qualify"|"refuse", "checks": {...},
    "refusals": [...], "toolchain_disclosures": [...]}``.
    """
    shape_ok, shape_detail, manifest = _evaluate_shape(recorded)
    disclosures = (
        sorted(
            disclosure_reasons(
                manifest.get("toolchain"),
                manifest.get("recipe", {}).get("effective_seed"),
            )
        )
        if manifest is not None
        else []
    )
    results = {
        "shape_valid": (shape_ok, shape_detail),
        "metrics_domain": _evaluate_metrics(metrics),
        "coverage_nonzero": _evaluate_coverage(coverage_percent, coverage_expected),
        "manifest_matches_intended": _evaluate_intended(
            manifest, shape_ok, expected_manifest
        ),
        "declared_bytes_current": _evaluate_declared_bytes(
            manifest, shape_ok, current_files
        ),
        "output_integrity": (
            _evaluate_artifacts(artifact_inventory, artifact_paths)
            + _evaluate_report_pin(report_bytes, report_sha256_pin)
        ),
        "partial_coverage_policy": _evaluate_partial_policy(
            manifest, shape_ok, allow_partial_inputs, disclosures
        ),
    }
    checks = {}
    refusals = []
    for name in CONSUMER_CHECKS:
        outcome = results[name]
        if isinstance(outcome, list):  # output integrity aggregates failures
            passed, detail = (
                not outcome,
                "; ".join(outcome)
                or ("cited artifacts and report pin verified against exact bytes"),
            )
        else:
            passed, detail = outcome
        checks[name] = {"passed": passed, "detail": detail}
        if not passed:
            refusals.append(name)
    return {
        "verdict": "qualify" if not refusals else "refuse",
        "checks": checks,
        "refusals": refusals,
        "toolchain_disclosures": disclosures,
    }


# --------------------------------------------------------------------------- #
# One-command golden-vector check
# --------------------------------------------------------------------------- #

_FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "functional_verification_identity_v1"
)


def check_golden_vectors(fixtures_dir=_FIXTURES_DIR):
    """Validate the bundled golden vectors; returns a list of failure
    strings (empty means every vector matched its published digest)."""
    fixtures_dir = Path(fixtures_dir)
    golden = json.loads(
        (fixtures_dir / "manifests" / "golden_digests.json").read_text(encoding="utf-8")
    )
    failures = []
    for vector in golden["vectors"]:
        raw = (fixtures_dir / "manifests" / vector["manifest"]).read_bytes()
        try:
            manifest = parse_manifest_bytes(raw)
            validate_input_manifest(manifest)
            canonical_digest = sha256_bytes(canonical_bytes(manifest))
            digest = input_sha256(manifest)
        except ContractError as exc:
            failures.append(f"{vector['manifest']}: {exc}")
            continue
        if canonical_digest != vector["canonical_manifest_sha256"]:
            failures.append(
                f"{vector['manifest']}: canonical bytes digest mismatch "
                f"(expected {vector['canonical_manifest_sha256']}, "
                f"got {canonical_digest})"
            )
        if digest != vector["input_sha256"]:
            failures.append(
                f"{vector['manifest']}: input_sha256 mismatch "
                f"(expected {vector['input_sha256']}, got {digest})"
            )
    return failures


def main():
    failures = check_golden_vectors()
    fixtures_dir = _FIXTURES_DIR
    golden = json.loads(
        (fixtures_dir / "manifests" / "golden_digests.json").read_text(encoding="utf-8")
    )
    print("klt-functional-verification-input-v1 golden vectors")
    for vector in golden["vectors"]:
        print(f"  {vector['manifest']}")
        print(f"    canonical_manifest_sha256 = {vector['canonical_manifest_sha256']}")
        print(f"    input_sha256              = {vector['input_sha256']}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("all golden vectors match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
