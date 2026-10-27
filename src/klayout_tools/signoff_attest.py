"""Built-in attestation producer for T1 items 1, 9 and 10 (issue #2844).

``klt signoff --attest {1,9,10} --inventory FILE`` reads a versioned
**attestation inventory**, runs the item-specific mechanical checks it
declares, and emits the artifact-anchored ``"generic"`` envelope that
``klt signoff --manifest`` already knows how to bind (issue #2718 --
:data:`~klayout_tools.signoff._ITEMS_REQUIRING_ANCHORED_GENERIC_EVIDENCE`,
:func:`~klayout_tools.signoff._generic_item_binding_refusal`,
:func:`~klayout_tools.signoff._generic_artifact_binding`). No second envelope
format: the output *is* a generic envelope, graded by the existing manifest
path with no special bypass.

Why it exists. Before this module a block that wanted an honest ``met`` on
items 1/9/10 had to hand-write the inventory, a hash-computing generator, a
drift check, and the envelope wrapper -- and the envelope's ``status:
"pass"`` was the author's own assertion. Here ``status`` is *derived*: it is
``"pass"`` only when every check this module ran passed, and the envelope's
``attestation.checks[]`` names each one.

What each item checks (see ``docs/cli/signoff.md``, "Attesting items 1, 9
and 10"):

- **Item 1 (Design sources)** -- every listed source exists; every declared
  derived artifact exists *and* its declared freshness command (e.g. ``klt
  netlist ... --check``) exits ``0``. A present-but-stale netlist fails.
- **Item 9 (Testbenches shipped)** -- every listed testbench exists; the
  declared cold-start invocation exits ``0``; the PDK name + revision and at
  least one tool version are pinned.
- **Item 10 (Repo hygiene)** -- every listed hygiene artifact exists; every
  declared CI/audit command exits ``0``.

Two failure classes, deliberately distinct:

- a **malformed inventory** (unknown ``schema_version``, wrong ``t1_item``,
  empty/duplicate path lists, a path outside the repository or through a
  symlink, a wrong-typed field) raises :class:`AttestationError` -- the
  CLI's ordinary error envelope, exit ``1``; nothing was attested.
- a **failed check** (a missing file, a stale derived artifact, a failed
  cold start or CI command, a missing pin) still yields an envelope -- with
  ``status: "fail"`` -- so the caller sees exactly which check failed. The
  CLI exits ``3`` and never writes ``--output`` for it.

Hashing is raw-byte SHA-256 (``sha256:<hex>``), the same
:func:`~klayout_tools._provenance.sha256_file` convention the grader re-hashes
with. Nothing is normalised.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from ._provenance import sha256_file
from ._report_verify import (
    VOLATILE_PROVENANCE_PATHS,
    build_rerun_result,
    load_committed_report,
)
from .build_identity import build_version, klayout_version
from .env_provenance import find_repo_root
from .signoff import (
    _COMMAND_EVIDENCE_TIMEOUT_S,
    _HASH_KIND_BYTES,
    _HASH_KIND_GEOMETRY,
    _ITEMS_REQUIRING_ANCHORED_GENERIC_EVIDENCE,
    _ITEMS_REQUIRING_ATTESTED_EVIDENCE,
    _REGENERATED_HASH_RE,
    SCHEMA_VERSION,
    SignoffError,
    _classify,
    _finite_json_float,
    _generic_item_binding_refusal,
    _reject_json_constant,
)

#: The attestation-inventory schema versions this build reads. An inventory
#: declaring any other version is refused (exit ``1``) rather than read with
#: guessed semantics -- the inventory is a public, versioned contract
#: (``docs/json-contract.md``).
INVENTORY_SCHEMA_VERSION = 1
SUPPORTED_INVENTORY_SCHEMA_VERSIONS: frozenset[int] = frozenset(
    {INVENTORY_SCHEMA_VERSION}
)

#: The literal ``kind`` every inventory must declare, so an unrelated JSON
#: file (a manifest, an envelope) handed to ``--inventory`` is refused rather
#: than half-read.
INVENTORY_KIND = "t1-attestation-inventory"

#: The ``attestation.producer`` value every envelope this module emits
#: carries -- how a reader (or a fleet roll-up) tells a derived pass from a
#: hand-written generic envelope.
PRODUCER = "klt signoff --attest"

#: The one hashing rule this producer applies: SHA-256 of the file's raw
#: bytes, ``sha256:``-prefixed. Recorded in every envelope so the rule is
#: part of the evidence, not an implicit convention.
HASH_ALGORITHM = "sha256-raw-bytes"

#: The T1 items this producer attests -- exactly the items whose evidence
#: :data:`~klayout_tools.signoff._ITEMS_REQUIRING_ATTESTED_EVIDENCE` restricts
#: to a generic envelope. Item 2 (Layout) stays out of scope: its binding is
#: the committed stream itself (issue #2718), unchanged by issue #2844.
ATTESTABLE_ITEMS: tuple[int, ...] = tuple(sorted(_ITEMS_REQUIRING_ATTESTED_EVIDENCE))

#: The one T1 item attested from a committed layout stream rather than an
#: inventory (issue #4188): ``--attest 2 --input STREAM``. Item 2's binding is
#: the stream itself, so there is no inventory to hand-write.
STREAM_ATTESTABLE_ITEM = 2

#: ``--hash-kind`` values a stream attestation's regeneration claim accepts.
REGENERATION_HASH_KINDS: tuple[str, ...] = (_HASH_KIND_BYTES, _HASH_KIND_GEOMETRY)

_ITEM_TITLES = {
    1: "Design sources",
    2: "Layout",
    9: "Testbenches shipped",
    10: "Repo hygiene",
}

#: Top-level inventory keys each item accepts. Anything else is refused --
#: an unknown key is far more likely a typo (``testbench`` for
#: ``testbenches``) than intent, and silently ignoring it would attest an
#: inventory the author did not write.
_COMMON_KEYS = frozenset({"schema_version", "kind", "t1_item", "description"})
_ITEM_KEYS: dict[int, frozenset[str]] = {
    1: frozenset({"sources", "derived"}),
    9: frozenset({"testbenches", "cold_start", "pins"}),
    10: frozenset({"hygiene", "checks"}),
}

#: The path lists each item declares, with the ``role`` each entry's check
#: records.
_PATH_LISTS: dict[int, tuple[tuple[str, str], ...]] = {
    1: (("sources", "source"),),
    9: (("testbenches", "testbench"),),
    10: (("hygiene", "hygiene"),),
}

#: Volatile envelope paths ``--check`` excludes from drift: the producing
#: build's version strings. A `klt` upgrade that attests byte-identical
#: inputs with identical outcomes is not evidence drift.
VOLATILE_ATTESTATION_PATHS = VOLATILE_PROVENANCE_PATHS


class AttestationError(SignoffError):
    """A malformed inventory or committed envelope -- nothing was attested.

    A :class:`~klayout_tools.signoff.SignoffError` subclass so the CLI's
    existing ``except SignoffError`` reports it through the ordinary error
    envelope (exit ``1``)."""


# --------------------------------------------------------------------------- #
# Inventory reading and structural validation
# --------------------------------------------------------------------------- #


def _read_inventory(path: str) -> tuple[dict[str, Any], str]:
    """Read ``path`` once and return ``(inventory, content_hash)``.

    The hash and the parse are taken from the **same** bytes, so the
    envelope can never record the hash of one revision of the inventory and
    the checks of another."""
    if path == "-":
        raise AttestationError(
            "--inventory must be a file path, not '-': the inventory is the "
            "artifact the envelope binds to, so it needs a path to pin"
        )
    if not os.path.exists(path):
        raise AttestationError(f"inventory not found: {path}")
    if os.path.islink(path):
        raise AttestationError(
            f"inventory '{path}' is a symlink -- attest the real file so the "
            "pinned path names the bytes that were hashed"
        )
    if not os.path.isfile(path):
        raise AttestationError(f"inventory is not a file: {path}")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise AttestationError(f"could not read inventory '{path}': {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        inventory = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise AttestationError(f"inventory '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(inventory, dict):
        raise AttestationError(
            f"inventory '{path}' must be a JSON object, got {type(inventory).__name__}"
        )
    return inventory, f"sha256:{digest}"


def _validate_header(inventory: Mapping[str, Any], item: int, source: str) -> None:
    version = inventory.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise AttestationError(
            f"inventory '{source}' has no integer 'schema_version' (this build "
            f"reads {sorted(SUPPORTED_INVENTORY_SCHEMA_VERSIONS)})"
        )
    if version not in SUPPORTED_INVENTORY_SCHEMA_VERSIONS:
        raise AttestationError(
            f"inventory '{source}' declares schema_version {version}, which this "
            f"build does not read (supported: "
            f"{sorted(SUPPORTED_INVENTORY_SCHEMA_VERSIONS)}) -- upgrade klt or "
            "write the inventory against a supported version"
        )
    if inventory.get("kind") != INVENTORY_KIND:
        raise AttestationError(
            f'inventory \'{source}\' must declare "kind": "{INVENTORY_KIND}"'
        )
    declared = inventory.get("t1_item")
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise AttestationError(
            f"inventory '{source}' must declare an integer 't1_item'"
        )
    if declared != item:
        raise AttestationError(
            f"inventory '{source}' declares t1_item {declared}, but --attest "
            f"asked for item {item} -- one inventory attests exactly one item"
        )
    allowed = _COMMON_KEYS | _ITEM_KEYS[item]
    unknown = sorted(set(inventory) - allowed)
    if unknown:
        raise AttestationError(
            f"inventory '{source}' has unknown field(s) for item {item}: "
            f"{', '.join(unknown)} (accepted: {', '.join(sorted(allowed))})"
        )
    description = inventory.get("description")
    if description is not None and not isinstance(description, str):
        raise AttestationError(
            f"inventory '{source}' field 'description' must be a string"
        )


def _normalize_repo_path(raw: Any, field: str, root: str) -> str:
    """Validate one inventory path and return its normalised, ``/``-separated
    repo-relative form.

    Refused: a non-string or empty value, an absolute path, one that escapes
    the repository (``..``), and one that traverses a symlink anywhere along
    its length -- a symlink would let the pinned path name bytes that live
    somewhere the repository does not control."""
    if not isinstance(raw, str) or not raw.strip():
        raise AttestationError(f"'{field}' entries must be non-empty path strings")
    if os.path.isabs(raw) or raw.startswith(("/", "\\")):
        raise AttestationError(
            f"'{field}' path '{raw}' is absolute -- inventory paths are "
            "repository-relative"
        )
    normalized = os.path.normpath(raw).replace(os.sep, "/")
    if normalized == "." or normalized == ".." or normalized.startswith("../"):
        raise AttestationError(
            f"'{field}' path '{raw}' is outside the repository ({root})"
        )
    absolute = os.path.join(root, normalized)
    if os.path.realpath(absolute) != os.path.normpath(absolute):
        raise AttestationError(
            f"'{field}' path '{raw}' traverses a symlink -- list the real path "
            "inside the repository"
        )
    return normalized


def _path_list(
    inventory: Mapping[str, Any], field: str, root: str, seen: dict[str, str]
) -> list[str]:
    value = inventory.get(field)
    if not isinstance(value, list):
        raise AttestationError(f"inventory field '{field}' must be a list of paths")
    if not value:
        raise AttestationError(
            f"inventory field '{field}' is empty -- an empty inventory attests nothing"
        )
    paths = []
    for raw in value:
        path = _normalize_repo_path(raw, field, root)
        if path in seen:
            raise AttestationError(
                f"path '{path}' is listed more than once (in '{seen[path]}' and "
                f"'{field}') -- each inventoried file must appear exactly once"
            )
        seen[path] = field
        paths.append(path)
    return paths


def _command_spec(raw: Any, field: str, root: str) -> dict[str, Any]:
    """Validate one declared command: ``{"argv": [str, ...], "cwd"?: path}``.

    ``argv`` is run directly (never through a shell) with ``cwd`` resolved
    against the repository root. Optional ``local_only: true`` marks a check
    that needs a toolchain the ``--check`` runner may lack: ``--attest`` runs
    it, ``--attest --check`` skips it and carries the committed outcome."""
    if not isinstance(raw, Mapping):
        raise AttestationError(f"'{field}' must be an object with an 'argv' list")
    argv = raw.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) and arg for arg in argv)
    ):
        raise AttestationError(
            f"'{field}.argv' must be a non-empty list of non-empty strings"
        )
    cwd_raw = raw.get("cwd", ".")
    if cwd_raw == ".":
        cwd = "."
    else:
        cwd = _normalize_repo_path(cwd_raw, f"{field}.cwd", root)
    spec: dict[str, Any] = {"argv": list(argv), "cwd": cwd}
    local_only = raw.get("local_only", False)
    if not isinstance(local_only, bool):
        raise AttestationError(f"'{field}.local_only' must be a boolean")
    if local_only:
        spec["local_only"] = True
    return spec


def _validated_inventory(
    inventory: Mapping[str, Any], item: int, root: str, source: str
) -> dict[str, Any]:
    """Structurally validate ``inventory`` for ``item`` and return the
    normalised form the checks run against. Raises :class:`AttestationError`
    for anything malformed."""
    _validate_header(inventory, item, source)
    seen: dict[str, str] = {}
    normalized: dict[str, Any] = {}
    for field, _role in _PATH_LISTS[item]:
        normalized[field] = _path_list(inventory, field, root, seen)
    if item == 1:
        normalized["derived"] = _validated_derived(inventory, root, seen)
    elif item == 9:
        if "cold_start" not in inventory:
            raise AttestationError(
                "item 9 inventory must declare 'cold_start' -- the invocation "
                "that reproduces the claimed measurements from a clean checkout"
            )
        normalized["cold_start"] = _command_spec(
            inventory["cold_start"], "cold_start", root
        )
        normalized["pins"] = inventory.get("pins")
    else:
        checks = inventory.get("checks")
        if not isinstance(checks, list) or not checks:
            raise AttestationError(
                "item 10 inventory must declare a non-empty 'checks' list -- the "
                "CI/audit commands whose result this attestation reports"
            )
        names: set[str] = set()
        specs = []
        for index, raw in enumerate(checks):
            spec = _command_spec(raw, f"checks[{index}]", root)
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                raise AttestationError(
                    f"'checks[{index}].name' must be a non-empty string"
                )
            if name in names:
                raise AttestationError(
                    f"check name '{name}' is declared more than once"
                )
            names.add(name)
            specs.append({"name": name, **spec})
        normalized["checks"] = specs
    return normalized


def _validated_derived(
    inventory: Mapping[str, Any], root: str, seen: dict[str, str]
) -> list[dict[str, Any]]:
    derived = inventory.get("derived")
    if not isinstance(derived, list) or not derived:
        raise AttestationError(
            "item 1 inventory must declare a non-empty 'derived' list -- the "
            "netlist(s) generated from the sources, each with a freshness check"
        )
    entries = []
    for index, raw in enumerate(derived):
        field = f"derived[{index}]"
        if not isinstance(raw, Mapping):
            raise AttestationError(f"'{field}' must be an object")
        unknown = sorted(set(raw) - {"path", "generator", "check"})
        if unknown:
            raise AttestationError(
                f"'{field}' has unknown field(s): {', '.join(unknown)}"
            )
        path = _normalize_repo_path(raw.get("path"), f"{field}.path", root)
        if path in seen:
            raise AttestationError(
                f"path '{path}' is listed more than once (in '{seen[path]}' and "
                f"'{field}') -- each inventoried file must appear exactly once"
            )
        seen[path] = field
        generator = raw.get("generator")
        if not isinstance(generator, str) or not generator:
            raise AttestationError(
                f"'{field}.generator' must name what produced the artifact "
                '(e.g. "klt netlist")'
            )
        if "check" not in raw:
            raise AttestationError(
                f"'{field}' must declare a 'check' command that fails when the "
                "artifact is stale (e.g. klt netlist ... --check) -- presence "
                "alone does not prove a derived artifact is current"
            )
        entries.append(
            {
                "path": path,
                "generator": generator,
                "check": _command_spec(raw["check"], f"{field}.check", root),
            }
        )
    return sorted(entries, key=lambda entry: entry["path"])


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _file_check(root: str, path: str, role: str) -> dict[str, Any]:
    digest = sha256_file(os.path.join(root, path))
    return {
        "check": "present",
        "role": role,
        "path": path,
        "passed": digest is not None,
        "content_hash": f"sha256:{digest}" if digest is not None else None,
    }


# Bounded tail of a failing command's captured output, for the text renderer
# only. Held out of band (never in a check dict) because it is host-specific
# and the envelope -- and its content hash -- must stay reproducible.
_OUTPUT_TAIL_MAX_LINES = 20
_OUTPUT_TAIL_MAX_BYTES = 2048
_failed_output: dict[tuple[tuple[str, ...], str], str] = {}


def _bounded_tail(text: str) -> str:
    lines = text.rstrip().splitlines()[-_OUTPUT_TAIL_MAX_LINES:]
    tail = "\n".join(lines)
    encoded = tail.encode("utf-8")
    if len(encoded) > _OUTPUT_TAIL_MAX_BYTES:
        tail = encoded[-_OUTPUT_TAIL_MAX_BYTES:].decode("utf-8", errors="replace")
    return tail


def failed_output_tail(check: Mapping[str, Any]) -> str:
    """Bounded tail of the captured stdout+stderr of the failing command
    ``check`` in the most recent attestation run, or ``""`` (a passing,
    skipped or unlaunchable check has none). Render-only: never part of the
    envelope."""
    return _failed_output.get(
        (tuple(check.get("argv") or ()), check.get("cwd", "")), ""
    )


def _run_command(root: str, spec: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Run one declared command; return ``(exit_status, error)``. ``error``
    is ``"launch_failed"``/``"timeout"`` (stable tokens, not host-specific
    messages, so the envelope stays reproducible) or ``None``. A failing
    command's output tail is kept for :func:`failed_output_tail`."""
    cwd = os.path.join(root, spec["cwd"]) if spec["cwd"] != "." else root
    key = (tuple(spec["argv"]), spec["cwd"])
    _failed_output.pop(key, None)
    try:
        completed = subprocess.run(
            spec["argv"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_COMMAND_EVIDENCE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "launch_failed"
    if completed.returncode != 0:
        tail = _bounded_tail(completed.stdout.decode("utf-8", errors="replace"))
        if tail:
            _failed_output[key] = tail
    return completed.returncode, None


def _command_check(
    root: str, spec: Mapping[str, Any], role: str, **identity: Any
) -> dict[str, Any]:
    exit_status, error = _run_command(root, spec)
    check: dict[str, Any] = {
        "check": "command",
        "role": role,
        **identity,
        "argv": list(spec["argv"]),
        "cwd": spec["cwd"],
        "exit_status": exit_status,
        "passed": exit_status == 0,
    }
    if error is not None:
        check["error"] = error
    if spec.get("local_only"):
        check["local_only"] = True
    return check


def _skipped_check(
    spec: Mapping[str, Any],
    role: str,
    committed_checks: Sequence[Mapping[str, Any]],
    **identity: Any,
) -> dict[str, Any]:
    """A ``local_only`` command that ``--check`` did not run: the committed
    check with the same identity is carried through (its outcome is the
    attest-time one, never a result of this run) and marked ``skipped``. With
    no committed counterpart the check is a failure -- nothing vouches for it."""
    for committed in committed_checks:
        if (
            committed.get("check") == "command"
            and committed.get("role") == role
            and list(committed.get("argv") or []) == list(spec["argv"])
            and all(committed.get(key) == value for key, value in identity.items())
        ):
            return {**committed, "local_only": True, "skipped": True}
    return {
        "check": "command",
        "role": role,
        **identity,
        "argv": list(spec["argv"]),
        "cwd": spec["cwd"],
        "exit_status": None,
        "passed": False,
        "local_only": True,
        "skipped": True,
    }


def _pin_checks(pins: Any) -> list[dict[str, Any]]:
    """Item 9's reproducibility pins: the PDK name + revision, and at least
    one tool version. A missing or empty pin is a failed check, not a
    malformed inventory -- the inventory is well-formed, it just does not
    yet say how to reproduce the run."""
    pins = pins if isinstance(pins, Mapping) else {}
    pdk = pins.get("pdk")
    pdk_value = None
    if isinstance(pdk, Mapping):
        name, revision = pdk.get("name"), pdk.get("revision")
        if isinstance(name, str) and name and isinstance(revision, str) and revision:
            pdk_value = {"name": name, "revision": revision}
    tools = pins.get("tools")
    tools_value = None
    if (
        isinstance(tools, Mapping)
        and tools
        and all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in tools.items()
        )
    ):
        tools_value = {key: tools[key] for key in sorted(tools)}
    return [
        {
            "check": "pin",
            "role": "pdk",
            "passed": pdk_value is not None,
            "value": pdk_value,
        },
        {
            "check": "pin",
            "role": "tools",
            "passed": tools_value is not None,
            "value": tools_value,
        },
    ]


def _run_checks(
    item: int,
    root: str,
    normalized: Mapping[str, Any],
    committed_checks: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every check for ``item``, in a deterministic order: file-presence
    checks sorted by path (independent of inventory listing order), then
    the item's command and pin checks. With ``committed_checks`` (the
    ``--check`` path) every ``local_only`` command is skipped, not run."""
    checks: list[dict[str, Any]] = []

    def command(spec: Mapping[str, Any], role: str, **identity: Any) -> dict[str, Any]:
        if committed_checks is not None and spec.get("local_only"):
            return _skipped_check(spec, role, committed_checks, **identity)
        return _command_check(root, spec, role, **identity)

    for field, role in _PATH_LISTS[item]:
        checks.extend(
            _file_check(root, path, role) for path in sorted(normalized[field])
        )
    if item == 1:
        for entry in normalized["derived"]:
            checks.append(_file_check(root, entry["path"], "derived"))
        for entry in normalized["derived"]:
            checks.append(
                command(
                    entry["check"],
                    "freshness",
                    target=entry["path"],
                    generator=entry["generator"],
                )
            )
    elif item == 9:
        checks.append(command(normalized["cold_start"], "cold_start"))
        checks.extend(_pin_checks(normalized["pins"]))
    else:
        for spec in normalized["checks"]:
            checks.append(
                command(
                    {k: v for k, v in spec.items() if k != "name"},
                    "audit",
                    name=spec["name"],
                )
            )
    return checks


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def build_attestation(
    item: int,
    inventory_path: str,
    committed_checks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """``klt signoff --attest ITEM --inventory PATH``: run every check
    ``PATH`` declares for T1 item ``item`` and return the artifact-anchored
    generic envelope (``status`` ``"pass"`` iff every check passed).

    Raises :class:`AttestationError` for a malformed inventory, an item this
    producer does not attest, or an inventory outside any git repository
    (inventory paths are repository-relative, and the envelope pins the
    inventory as a ``{"path", "scope": "repo"}`` object the grader resolves
    against the repository root).

    ``committed_checks`` is passed only by :func:`check_attestation`: it makes
    ``local_only`` commands skip (carrying the committed outcome, marked
    ``skipped``) instead of run."""
    if item not in _ITEMS_REQUIRING_ATTESTED_EVIDENCE:
        raise AttestationError(
            f"--attest supports T1 items {', '.join(map(str, ATTESTABLE_ITEMS))}; "
            f"got {item}"
        )
    inventory, inventory_hash = _read_inventory(inventory_path)
    absolute = os.path.realpath(os.path.abspath(inventory_path))
    root = find_repo_root(os.path.dirname(absolute))
    if root is None:
        raise AttestationError(
            f"inventory '{inventory_path}' is not inside a git repository -- "
            "inventory paths are repository-relative"
        )
    inventory_rel = os.path.relpath(absolute, root).replace(os.sep, "/")
    normalized = _validated_inventory(inventory, item, root, inventory_path)
    checks = _run_checks(item, root, normalized, committed_checks)
    passed_count = sum(1 for check in checks if check["passed"])
    status = "pass" if passed_count == len(checks) else "fail"
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "generic",
        "status": status,
        "t1_item": item,
        "summary": (
            f"T1 item {item} ({_ITEM_TITLES[item]}): {passed_count}/{len(checks)} "
            f"attestation checks passed ({PRODUCER})"
        ),
        "source": inventory_rel,
        "attestation": {
            "producer": PRODUCER,
            "inventory_schema_version": inventory["schema_version"],
            "hash_algorithm": HASH_ALGORITHM,
            "check_count": len(checks),
            "passed_count": passed_count,
            "checks": checks,
        },
        "provenance": {
            "klt_version": build_version(),
            "klayout_version": None,
            "pdk": None,
            "deck": None,
            "input": {
                "path": {"path": inventory_rel, "scope": "repo"},
                "content_hash": inventory_hash,
            },
        },
    }
    _self_check(envelope, item, inventory_hash)
    return envelope


def _self_check(envelope: Mapping[str, Any], item: int, content_hash: str) -> None:
    """Run the emitted envelope through the grader's own recogniser and
    binding check -- so this producer can never drift from what ``klt
    signoff --manifest`` accepts. A failure here is a bug in this module,
    not in the caller's inventory."""
    kind = _classify(envelope, "<attestation>")
    refusal = _generic_item_binding_refusal(
        kind, item, envelope, {"content_hash": content_hash}
    )
    if (  # pragma: no cover - invariant
        item not in _ITEMS_REQUIRING_ANCHORED_GENERIC_EVIDENCE
        or kind != "generic"
        or refusal is not None
    ):
        raise AttestationError(
            f"internal error: attestation envelope is not bindable ({kind}, {refusal})"
        )


def render_envelope(envelope: Mapping[str, Any]) -> str:
    """The exact bytes ``--output`` writes: two-space-indented JSON plus a
    trailing newline -- the same rendering ``--format json`` prints."""
    return json.dumps(envelope, indent=2) + "\n"


def write_attestation(envelope: Mapping[str, Any], output: str) -> bool:
    """Write ``envelope`` to ``output`` **only if its bytes would change**;
    return whether the file was written. Unchanged inputs leave a committed
    envelope untouched (mtime included)."""
    rendered = render_envelope(envelope).encode("utf-8")
    try:
        with open(output, "rb") as handle:
            if handle.read() == rendered:
                return False
    except OSError:
        pass
    directory = os.path.dirname(os.path.abspath(output))
    os.makedirs(directory, exist_ok=True)
    with open(output, "wb") as handle:
        handle.write(rendered)
    return True


def check_attestation(
    item: int, inventory_path: str, committed_path: str
) -> dict[str, Any]:
    """``klt signoff --attest ITEM --inventory PATH --check COMMITTED``:
    re-attest and diff against the committed envelope, writing nothing.

    Returns the shared ``--check`` drift shape (``{schema_version, mode,
    report, status, drift, fresh}``, :func:`~._report_verify.build_rerun_result`)
    plus ``fresh_status`` -- the re-attested envelope's own ``status``. The
    CLI exits ``0`` only for ``status: "match"`` *and* ``fresh_status:
    "pass"``: a faithfully reproduced failing attestation is still not a
    passing gate. Only the producing build's version strings are excluded
    from the diff (:data:`VOLATILE_ATTESTATION_PATHS`); a change to the
    inventory, to any inventoried file, to a check's outcome, or to the
    committed envelope itself is drift.

    A ``local_only`` command is not run: it is listed in ``skipped_checks``
    (never counted as passed by this run) and its committed outcome is carried
    into ``fresh_status``, so a committed failure still fails the gate."""
    committed = _load_committed_attestation(committed_path)
    committed_checks = (committed.get("attestation") or {}).get("checks")
    fresh = build_attestation(
        item,
        inventory_path,
        committed_checks if isinstance(committed_checks, list) else [],
    )
    return _drift_result(committed_path, committed, fresh)


def _load_committed_attestation(committed_path: str) -> dict[str, Any]:
    committed = load_committed_report(committed_path, AttestationError)
    if committed.get("kind") != "generic" or "attestation" not in committed:
        raise AttestationError(
            f"committed envelope '{committed_path}' is not a klt signoff --attest "
            'envelope (no "kind": "generic" with an \'attestation\' block) -- '
            "check the path"
        )
    return committed


def _drift_result(
    committed_path: str, committed: dict[str, Any], fresh: dict[str, Any]
) -> dict[str, Any]:
    # ``skipped`` marks a local_only check this run did not execute; its
    # (carried-through) outcome is the committed one, so it is reported beside
    # the diff rather than as a difference from the committed envelope.
    skipped = []
    for check in fresh["attestation"]["checks"]:
        if check.pop("skipped", False):
            skipped.append(
                {
                    key: check[key]
                    for key in ("role", "name", "target", "argv", "cwd", "passed")
                    if key in check
                }
            )
    result = build_rerun_result(
        report_path=committed_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_ATTESTATION_PATHS,
        mode="check",
    )
    fresh_payload = result.pop("fresh")
    result["fresh_status"] = fresh_payload["status"]
    result["skipped_checks"] = skipped
    result["fresh"] = fresh_payload
    return result


# --------------------------------------------------------------------------- #
# Item 2: attest a committed layout stream (issue #4188)
# --------------------------------------------------------------------------- #

#: A database unit counts as "on grid" when it is a positive length that
#: divides one micron into a whole number of database units (0.001 -> 1000),
#: within this relative tolerance for float representation.
_DBU_GRID_TOLERANCE = 1e-6


def _stream_checks(stream: str, digest: str) -> list[dict[str, Any]]:
    """The three item-2 checks, in fixed order: stream readable (loads in
    ``pya``), non-empty top cell(s), database unit on grid. A stream that
    cannot be read fails all three -- nothing else can be said about it."""
    layout = None
    try:
        import klayout.db as kdb

        layout = kdb.Layout()
        layout.read(stream)
    except Exception:
        layout = None
    checks: list[dict[str, Any]] = [
        {
            "check": "stream_readable",
            "role": "layout",
            "passed": layout is not None,
            "content_hash": digest,
        }
    ]
    top_names: list[str] = []
    non_empty = False
    dbu = None
    if layout is not None:
        tops = list(layout.top_cells())
        top_names = sorted(cell.name for cell in tops)
        non_empty = bool(tops) and all(not cell.bbox().empty() for cell in tops)
        dbu = float(layout.dbu)
    checks.append(
        {
            "check": "top_cell_non_empty",
            "role": "layout",
            "passed": non_empty,
            "top_cells": top_names,
        }
    )
    on_grid = False
    if dbu is not None and dbu > 0:
        per_micron = 1.0 / dbu
        on_grid = abs(per_micron - round(per_micron)) <= _DBU_GRID_TOLERANCE * max(
            per_micron, 1.0
        )
    checks.append(
        {"check": "dbu_on_grid", "role": "layout", "passed": on_grid, "dbu": dbu}
    )
    return checks


def _validate_regeneration_claim(
    generator: str | None, regenerated_hash: str | None, hash_kind: str
) -> None:
    """Refuse a malformed ``--generator`` / ``--regenerated-hash`` /
    ``--hash-kind`` combination with :class:`AttestationError`."""
    if (generator is None) != (regenerated_hash is None):
        raise AttestationError(
            "--generator and --regenerated-hash must be given together: a "
            "regeneration claim needs both the command and the hash it produced"
        )
    if hash_kind not in REGENERATION_HASH_KINDS:
        raise AttestationError(
            f"--hash-kind must be one of {', '.join(REGENERATION_HASH_KINDS)}; "
            f"got '{hash_kind}'"
        )
    if generator is not None and not generator.strip():
        raise AttestationError("--generator must be a non-empty command string")
    if regenerated_hash is not None and not _REGENERATED_HASH_RE.match(
        regenerated_hash
    ):
        raise AttestationError(
            "--regenerated-hash must be 'sha256:' followed by 64 lowercase hex digits"
        )


def _resolve_stream(stream_path: str) -> tuple[str, str]:
    """``(absolute path, repository-relative path)`` of a committed stream, or
    :class:`AttestationError` for a missing, symlinked, non-file or
    non-repository path."""
    if not os.path.exists(stream_path):
        raise AttestationError(f"stream not found: {stream_path}")
    if os.path.islink(stream_path):
        raise AttestationError(
            f"stream '{stream_path}' is a symlink -- attest the real file so "
            "the pinned path names the bytes that were hashed"
        )
    if not os.path.isfile(stream_path):
        raise AttestationError(f"stream is not a file: {stream_path}")
    absolute = os.path.realpath(os.path.abspath(stream_path))
    root = find_repo_root(os.path.dirname(absolute))
    if root is None:
        raise AttestationError(
            f"stream '{stream_path}' is not inside a git repository -- the "
            "envelope pins it as a repository-relative path"
        )
    return absolute, os.path.relpath(absolute, root).replace(os.sep, "/")


def build_stream_attestation(
    stream_path: str,
    *,
    generator: str | None = None,
    regenerated_hash: str | None = None,
    hash_kind: str = _HASH_KIND_BYTES,
) -> dict[str, Any]:
    """``klt signoff --attest 2 --input STREAM``: run the item-2 checks on a
    committed GDS/OASIS stream and return the artifact-anchored generic
    envelope (``status`` ``"pass"`` iff every check passed).

    ``generator`` and ``regenerated_hash`` must be given together; they
    populate ``provenance.regeneration`` exactly as the grader reads it
    (:func:`~.signoff._regeneration_claim`). The generator is echoed, never
    run. Raises :class:`AttestationError` for a missing, symlinked or
    non-repository stream or a malformed regeneration claim."""
    _validate_regeneration_claim(generator, regenerated_hash, hash_kind)
    absolute, stream_rel = _resolve_stream(stream_path)
    raw_digest = sha256_file(absolute)
    if raw_digest is None:
        raise AttestationError(f"could not read stream '{stream_path}'")
    stream_hash = f"sha256:{raw_digest}"
    checks = _stream_checks(absolute, stream_hash)
    passed_count = sum(1 for check in checks if check["passed"])
    status = "pass" if passed_count == len(checks) else "fail"
    provenance: dict[str, Any] = {
        "klt_version": build_version(),
        "klayout_version": klayout_version(),
        "pdk": None,
        "deck": None,
        "input": {
            "path": {"path": stream_rel, "scope": "repo"},
            "content_hash": stream_hash,
        },
    }
    if generator is not None:
        regeneration: dict[str, Any] = {
            "generator": generator,
            "regenerated_hash": regenerated_hash,
        }
        if hash_kind != _HASH_KIND_BYTES:
            regeneration["hash_kind"] = hash_kind
        provenance["regeneration"] = regeneration
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "generic",
        "status": status,
        "t1_item": STREAM_ATTESTABLE_ITEM,
        "summary": (
            f"T1 item 2 ({_ITEM_TITLES[2]}): {passed_count}/{len(checks)} "
            f"attestation checks passed ({PRODUCER})"
        ),
        "source": stream_rel,
        "attestation": {
            "producer": PRODUCER,
            "hash_algorithm": HASH_ALGORITHM,
            "check_count": len(checks),
            "passed_count": passed_count,
            "checks": checks,
        },
        "provenance": provenance,
    }
    _self_check(envelope, STREAM_ATTESTABLE_ITEM, stream_hash)
    return envelope


def check_stream_attestation(
    stream_path: str,
    committed_path: str,
    *,
    generator: str | None = None,
    regenerated_hash: str | None = None,
    hash_kind: str = _HASH_KIND_BYTES,
) -> dict[str, Any]:
    """``klt signoff --attest 2 --input STREAM --check COMMITTED``: re-attest
    and diff against the committed envelope, writing nothing. Same result
    shape as :func:`check_attestation`."""
    committed = _load_committed_attestation(committed_path)
    fresh = build_stream_attestation(
        stream_path,
        generator=generator,
        regenerated_hash=regenerated_hash,
        hash_kind=hash_kind,
    )
    return _drift_result(committed_path, committed, fresh)
