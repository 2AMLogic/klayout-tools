"""``klt signoff --allow-generator-exec``: run an *approved* generator on the
batch runner and byte-compare its output with the committed artifact
(issue #3289).

T1 items 1 and 2 may carry a ``provenance.regeneration`` claim (issue #2887)
that the attester re-ran a generator and got the committed bytes. That claim
is the attester's word; only running the generator proves it. This module is
the **opt-in, default-off** executing form, under the operator's trusted-code
ruling on #3289:

1. **Approval comes from outside the manifest.** The manifest only *requests*
   a generator (its ``regeneration.generator`` string). Whether that string
   may run is decided by an operator-supplied approvals file
   (``--generator-approvals FILE`` / ``$KLT_SIGNOFF_GENERATOR_APPROVALS``)
   that binds, per recipe, the interpreter, the script **path and content
   hash**, the exact allowed argument lists, and the committed artifact it
   regenerates. A manifest cannot add, widen or approve an entry, and an
   interpreter name alone (``python3``, ``bash``) matches nothing: the
   requested argv must be ``[interpreter, script, *allowed_arguments]``.
2. **Existing Spot lifecycle, no runner of its own.** Submit, launch, poll and
   collect are :mod:`klayout_tools.sim_batch`'s, with the git-tracked input
   tree packaged exactly as :mod:`klayout_tools.netlist_batch` packages one.
   Fleet/forge/LLM/submitter credentials are never placed in ``job.json``.
3. **No fallback.** Any submit, launch, poll, collect or packaging failure is
   a *failed execution* -- never ``met``, never ``executed_by_klt: true``,
   and the generator is never run on this host.
4. **Trusted-code policy, not isolation.** The job harness runs the approved
   command as root with outbound network and the job's S3 role; a Spot VM is
   not a sandbox for untrusted code. Only code the operator has approved by
   content hash runs at all.

This module imports nothing from :mod:`klayout_tools.signoff`; the grader
maps a :class:`GeneratorOutcome` onto its ``_REASON_*`` constants.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import _batch_common, _provenance, netlist_batch, remote_transport, sim_batch

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Operator-side location of the approvals file, used when
#: ``--generator-approvals`` is not given. Never read from a manifest.
APPROVALS_ENV = "KLT_SIGNOFF_GENERATOR_APPROVALS"

#: ``schema_version`` of the approvals file.
APPROVALS_SCHEMA_VERSION = 1

#: Default and ceiling for one generator run's inner ``timeout`` (seconds).
DEFAULT_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 3600.0

#: Default PDK variant selecting the runner image.
DEFAULT_PDK = "sky130A"

#: ``job.json``'s ``tool`` label.
BATCH_JOB_TOOL = "generator"

#: The single input the job receives under ``inputs/``.
TREE_FILENAME = "tree.tar.gz"

#: Files the job writes under ``$EDA_OUTPUT_DIR``.
OUTPUT_FILENAME = "generated.artifact"
OUTPUT_HASH_FILENAME = "generated.sha256"

#: Seconds the inner ``timeout`` waits after ``TERM`` before ``KILL``.
_KILL_AFTER_S = 10

#: ``failure`` values of a failed :class:`GeneratorOutcome`.
FAIL_PACKAGE = "package_failed"
FAIL_CONFIG = "batch_unconfigured"
FAIL_SUBMIT = "submit_failed"
FAIL_POLL_TIMEOUT = "poll_timeout"
FAIL_JOB_TIMEOUT = "job_timeout"
FAIL_JOB_FAILED = "job_failed"
FAIL_RUNNER = "runner_incompatible"
FAIL_COLLECT = "collect_failed"
FAIL_OUTPUT_MISSING = "output_missing"
FAIL_OUTPUT_INCOMPLETE = "output_incomplete"

#: ``state`` values of a :class:`GeneratorOutcome`.
STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_FAILED = "failed"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_INTERPRETER_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")

_APPROVAL_KEYS = frozenset(
    {
        "id",
        "interpreter",
        "script",
        "script_sha256",
        "allowed_arguments",
        "artifact",
        "output",
        "pdk",
        "timeout_s",
        "include",
    }
)

#: Stated in every execution disclosure.
NOT_CHECKED = (
    "generator correctness and the artifact's topical relevance are not "
    "checked; only that the approved generator reproduces the committed bytes"
)


class GeneratorApprovalError(Exception):
    """The trusted approvals file is missing, unreadable or malformed."""


# --------------------------------------------------------------------------- #
# Approvals (trusted configuration)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GeneratorApproval:
    """One operator-approved recipe. All paths are repo-root-relative POSIX."""

    id: str
    interpreter: str
    script: str
    script_sha256: str
    artifact: str
    output: str
    allowed_arguments: tuple[tuple[str, ...], ...] = ((),)
    pdk: str = DEFAULT_PDK
    timeout_s: float = DEFAULT_TIMEOUT_S
    include: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratorExecution:
    """The opt-in handed to the grader: trusted approvals plus the (test
    injectable) transport seams. Absent (``None``) means execution is off."""

    approvals: tuple[GeneratorApproval, ...]
    approvals_path: str
    runner: remote_transport.CommandRunner | None = None
    sleep: Callable[[float], None] = time.sleep


def _relative_posix(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise GeneratorApprovalError(f"{what} must be a non-empty string")
    if value.startswith("/") or os.path.isabs(value):
        raise GeneratorApprovalError(f"{what} must be repo-relative (got {value!r})")
    normal = posixpath.normpath(value.replace("\\", "/"))
    if normal in (".", "") or normal == ".." or normal.startswith("../"):
        raise GeneratorApprovalError(f"{what} must stay inside the repo ({value!r})")
    return normal


def _parse_approval(index: int, raw: Any) -> GeneratorApproval:
    label = f"approvals[{index}]"
    if not isinstance(raw, Mapping):
        raise GeneratorApprovalError(f"{label} must be an object")
    unknown = sorted(set(raw) - _APPROVAL_KEYS)
    if unknown:
        raise GeneratorApprovalError(f"{label} has unknown keys: {', '.join(unknown)}")
    ident = raw.get("id")
    if not isinstance(ident, str) or not _ID_RE.match(ident):
        raise GeneratorApprovalError(f"{label}.id must match [A-Za-z0-9._-]+")
    interpreter = raw.get("interpreter")
    if not isinstance(interpreter, str) or not _INTERPRETER_RE.match(interpreter):
        raise GeneratorApprovalError(
            f"{label}.interpreter must be a bare command name or path without "
            "whitespace or shell metacharacters"
        )
    script = _relative_posix(raw.get("script"), f"{label}.script")
    script_sha256 = raw.get("script_sha256")
    if not isinstance(script_sha256, str) or not _SHA256_RE.match(script_sha256):
        raise GeneratorApprovalError(
            f"{label}.script_sha256 is required: sha256:<64 lowercase hex> of "
            "the approved script's bytes (an approval binds content, not a name)"
        )
    allowed_raw = raw.get("allowed_arguments", [[]])
    if not isinstance(allowed_raw, list) or not all(
        isinstance(item, list) and all(isinstance(arg, str) for arg in item)
        for item in allowed_raw
    ):
        raise GeneratorApprovalError(
            f"{label}.allowed_arguments must be a list of string lists"
        )
    timeout_s = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not 0 < timeout_s <= MAX_TIMEOUT_S
    ):
        raise GeneratorApprovalError(
            f"{label}.timeout_s must be a number in (0, {int(MAX_TIMEOUT_S)}]"
        )
    pdk = raw.get("pdk", DEFAULT_PDK)
    if not isinstance(pdk, str) or not _ID_RE.match(pdk):
        raise GeneratorApprovalError(f"{label}.pdk must be a PDK variant name")
    include_raw = raw.get("include", [])
    if not isinstance(include_raw, list) or not all(
        isinstance(item, str) and item for item in include_raw
    ):
        raise GeneratorApprovalError(f"{label}.include must be a list of globs")
    return GeneratorApproval(
        id=ident,
        interpreter=interpreter,
        script=script,
        script_sha256=script_sha256,
        artifact=_relative_posix(raw.get("artifact"), f"{label}.artifact"),
        output=_relative_posix(raw.get("output"), f"{label}.output"),
        allowed_arguments=tuple(tuple(item) for item in allowed_raw),
        pdk=pdk,
        timeout_s=float(timeout_s),
        include=tuple(include_raw),
    )


def load_approvals(path: str) -> tuple[GeneratorApproval, ...]:
    """Read and strictly validate the trusted approvals file."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise GeneratorApprovalError(
            f"cannot read generator approvals file {path}: {exc}"
        ) from exc
    except ValueError as exc:
        raise GeneratorApprovalError(
            f"generator approvals file {path} is not valid JSON: {exc}"
        ) from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != APPROVALS_SCHEMA_VERSION
        or not isinstance(document.get("approvals"), list)
        or set(document) - {"schema_version", "approvals"}
    ):
        raise GeneratorApprovalError(
            f"generator approvals file {path} must be an object "
            f'{{"schema_version": {APPROVALS_SCHEMA_VERSION}, "approvals": [...]}}'
        )
    approvals = tuple(
        _parse_approval(index, raw) for index, raw in enumerate(document["approvals"])
    )
    ids = [approval.id for approval in approvals]
    if len(set(ids)) != len(ids):
        raise GeneratorApprovalError(
            f"generator approvals file {path} repeats an approval id"
        )
    return approvals


def resolve_execution(
    allow: bool,
    approvals_path: str | None,
    *,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GeneratorExecution | None:
    """The opt-in: ``None`` (execution off) unless ``allow`` is set.

    With ``allow``, the approvals file is mandatory (``approvals_path`` or
    ``$KLT_SIGNOFF_GENERATOR_APPROVALS``) -- opting in without a trusted
    approval source is a configuration error, not an implicit allow-all. A
    path with no opt-in is refused rather than silently inert.
    """
    env_path = os.environ.get(APPROVALS_ENV, "").strip() or None
    if not allow:
        if approvals_path is not None:
            raise GeneratorApprovalError(
                "--generator-approvals only applies with --allow-generator-exec"
            )
        return None
    path = approvals_path or env_path
    if path is None:
        raise GeneratorApprovalError(
            "--allow-generator-exec needs trusted approvals outside the "
            f"manifest: pass --generator-approvals FILE or set ${APPROVALS_ENV}"
        )
    return GeneratorExecution(
        approvals=load_approvals(path),
        approvals_path=path,
        runner=runner,
        sleep=sleep,
    )


def match_approval(
    approvals: Sequence[GeneratorApproval], generator: str, artifact: str
) -> tuple[GeneratorApproval, tuple[str, ...]] | None:
    """The approval (and the matched argument list) permitting the manifest's
    requested ``generator`` to regenerate repo-relative ``artifact``, else
    ``None``.

    Exact comparison of the whole argv after ``shlex`` splitting and path
    normalisation: ``argv[0]`` must be the approved interpreter, ``argv[1]``
    the approved script, and the rest one approved argument list. An
    interpreter alone, a different script, a different argument or a
    different artifact matches nothing.
    """
    try:
        argv = shlex.split(generator)
    except ValueError:
        return None
    if len(argv) < 2:
        return None
    script = argv[1].replace("\\", "/")
    if not script.startswith("/"):
        script = posixpath.normpath(script)
    arguments = tuple(argv[2:])
    wanted = posixpath.normpath(artifact)
    for approval in approvals:
        if (
            argv[0] == approval.interpreter
            and script == approval.script
            and arguments in approval.allowed_arguments
            and wanted == approval.artifact
        ):
            return approval, arguments
    return None


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #


@dataclass
class GeneratorOutcome:
    """What one approved execution established.

    ``state`` is ``fresh`` (collected output byte-identical to the committed
    artifact), ``stale`` (it differs) or ``failed`` (no usable execution
    evidence; ``failure`` says why). ``executed_by_klt`` is true only for
    ``fresh`` and ``stale``. ``evidence`` is the additive JSON disclosure.
    """

    state: str
    failure: str | None = None
    message: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def executed_by_klt(self) -> bool:
        return self.state in (STATE_FRESH, STATE_STALE)


def _fail(failure: str, message: str, evidence: dict[str, Any]) -> GeneratorOutcome:
    return GeneratorOutcome(
        STATE_FAILED,
        failure=failure,
        message=message,
        evidence={
            **evidence,
            "executed_by_klt": False,
            "failure": failure,
            "message": message,
        },
    )


# --------------------------------------------------------------------------- #
# Packaging and job spec
# --------------------------------------------------------------------------- #


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _select_tree(root: str, approval: GeneratorApproval) -> netlist_batch.BatchTree:
    """The git-tracked files the approval ships, capped in size; the approved
    script must be among them."""
    resolved = os.path.realpath(root)
    files = netlist_batch._tracked_files(resolved)
    selected = netlist_batch._select_files(files, approval.include, ())
    if approval.script not in files:
        raise netlist_batch.NetlistError(
            f"approved script {approval.script} is not tracked by git under the root"
        )
    if approval.script not in selected:
        raise netlist_batch.NetlistError(
            f"approved script {approval.script} is left out by the approval's include"
        )
    present: list[str] = []
    total = 0
    for rel in selected:
        path = os.path.join(resolved, rel)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            continue
        present.append(rel)
        total += info.st_size
    if total > netlist_batch.MAX_BATCH_TREE_BYTES:
        raise netlist_batch.NetlistError(
            f"generator input tree is {total} bytes, above the "
            f"{netlist_batch.MAX_BATCH_TREE_BYTES}-byte cap; narrow the "
            "approval's include globs",
            code=netlist_batch.BATCH_INPUT_TOO_LARGE_CODE,
        )
    return netlist_batch.BatchTree(
        root=resolved,
        files=tuple(present),
        total_bytes=total,
        schematic=approval.script,
        rcfile_arg=None,
        rcfile_pdk_relative=False,
    )


def _package(
    root: str, approval: GeneratorApproval, scratch: str
) -> tuple[str, dict[str, Any]]:
    """Archive the git-tracked tree and verify the approved script *as
    shipped*. Returns ``(tarball_path, input_tree_provenance)``; raises
    :class:`netlist_batch.NetlistError` for a packaging refusal."""
    tree = _select_tree(root, approval)
    tarball = os.path.join(scratch, TREE_FILENAME)
    netlist_batch.build_tarball(tree, tarball)

    digest = hashlib.sha256()
    shipped_script: bytes | None = None
    with tarfile.open(tarball, "r:gz") as archive:
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                digest.update(f"{member.name}\0{member.type!r}\n".encode())
                continue
            handle = archive.extractfile(member)
            data = handle.read() if handle is not None else b""
            digest.update(f"{member.name}\0{_sha256_bytes(data)}\n".encode())
            if member.name == approval.script:
                shipped_script = data
    if (
        shipped_script is None
        or _sha256_bytes(shipped_script) != approval.script_sha256
    ):
        raise netlist_batch.NetlistError(
            f"approved script {approval.script} does not hash to the approved "
            f"{approval.script_sha256}; it changed since it was approved"
        )
    return tarball, {
        "file_count": len(tree.files),
        "total_bytes": tree.total_bytes,
        "files_sha256": "sha256:" + digest.hexdigest(),
    }


def build_job_spec(
    approval: GeneratorApproval,
    arguments: Sequence[str],
    *,
    tarball_path: str,
    client_version: str | None = None,
    version_check: str = "enforce",
) -> sim_batch.BatchJobSpec:
    """The generator job as a :class:`~klayout_tools.sim_batch.BatchJobSpec`.

    ``cmd`` is the runner-version preflight (the same one ``klt sim`` and
    ``klt netlist`` use), then: extract the tree into a fresh directory and
    run the approved argv there under ``timeout``; on a clean exit copy the
    declared output file and its hash into ``$EDA_OUTPUT_DIR``. Every argv
    element is ``shlex``-quoted; nothing the manifest wrote reaches the shell
    except through the approved ``interpreter`` / ``script`` / argument
    strings. No credential is read from, or written to, the job environment.
    """
    version = (
        client_version if client_version is not None else _provenance._klt_version()
    )
    preflight = sim_batch._runner_preflight_script(
        version, enforce=version_check == "enforce"
    )
    out = '"$EDA_OUTPUT_DIR/{}"'
    setup = " && ".join(
        [
            'EDA_INPUT_DIR=$(cd "$EDA_INPUT_DIR" && pwd -P)',
            'EDA_OUTPUT_DIR=$(cd "$EDA_OUTPUT_DIR" && pwd -P)',
            "w=$(mktemp -d)",
            f'tar -xzf "$EDA_INPUT_DIR/{TREE_FILENAME}" -C "$w"',
            'cd "$w"',
        ]
    )
    argv = " ".join(
        shlex.quote(item)
        for item in (approval.interpreter, approval.script, *arguments)
    )
    run = (
        f"timeout -k {_KILL_AFTER_S} {int(approval.timeout_s)} {argv}"
        f" > {out.format('generator.stdout')}"
        f" 2> >(tee {out.format(sim_batch.BATCH_STDERR_FILENAME)} >&2)"
    )
    publish = (
        f"if [ $rc -eq 0 ]; then cp -- {shlex.quote(approval.output)} "
        f"{out.format(OUTPUT_FILENAME)} && "
        f"printf 'sha256:%s\\n' \"$(sha256sum {out.format(OUTPUT_FILENAME)} "
        f"| cut -d' ' -f1)\" > {out.format(OUTPUT_HASH_FILENAME)}; rc=$?; fi"
    )
    command = (
        preflight
        + setup
        + " && "
        + run
        + "; rc=$?; wait $! 2>/dev/null; "
        + publish
        + "; exit $rc"
    )
    return sim_batch.BatchJobSpec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=approval.pdk,
        timeout_seconds=netlist_batch._job_timeout_seconds(approval.timeout_s),
        inputs=(
            sim_batch.BatchJobInput(
                name=TREE_FILENAME, label="generator tree", local_path=tarball_path
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _runner_rejection(collected: str) -> str | None:
    """The runner-version preflight's message when ``report.json`` holds its
    ``batch_runner_*`` rejection envelope, else ``None``."""
    try:
        kind, error = sim_batch._classify_collected_report(
            sim_batch.read_collected_report(collected)
        )
    except sim_batch.BatchError:
        return None
    if kind == "error" and error is not None:
        code = str(error.get("code") or "")
        if code.startswith("batch_runner_"):
            return f"{code}: {error.get('message')}"
    return None


def run_approved_generator(
    execution: GeneratorExecution,
    approval: GeneratorApproval,
    arguments: Sequence[str],
    *,
    root: str,
    committed_hash: str,
    artifact: str,
) -> GeneratorOutcome:
    """Run ``approval`` on the batch runner and compare its output with the
    committed artifact (``committed_hash`` is what ``artifact`` just
    re-hashed to). Never raises for an execution problem: every failure is a
    ``failed`` outcome, and no path here runs the generator on this host.
    """
    evidence: dict[str, Any] = {
        "backend": "batch",
        "recipe": approval.id,
        "command": [approval.interpreter, approval.script, *arguments],
        "script": approval.script,
        "script_sha256": approval.script_sha256,
        "artifact": artifact,
        "committed_hash": committed_hash,
        "output": approval.output,
        "not_checked": NOT_CHECKED,
    }
    try:
        config = sim_batch._resolve_batch_config(
            {"models": {"pdk": approval.pdk}},
            corner_count=1,
            timeout_s=approval.timeout_s,
        )
    except Exception as exc:  # SimError / BatchError: unresolvable fleet config
        return _fail(FAIL_CONFIG, f"batch backend not usable: {exc}", evidence)

    job_id = _batch_common.make_job_id("signoff-gen")
    with tempfile.TemporaryDirectory(prefix="klt-signoff-generator-") as scratch:
        try:
            tarball, input_tree = _package(root, approval, scratch)
            spec = build_job_spec(
                approval,
                arguments,
                tarball_path=tarball,
                version_check=config.runner_version_check,
            )
        except (netlist_batch.NetlistError, sim_batch.BatchError, OSError) as exc:
            return _fail(FAIL_PACKAGE, str(exc), evidence)
        evidence["input_tree"] = input_tree

        try:
            sim_batch.submit_job(
                config, job_id, spec, runner=execution.runner, progress=False
            )
            sim_batch.launch_job(
                config, job_id, runner=execution.runner, sleep=execution.sleep
            )
            status = sim_batch.poll_status(
                config,
                job_id,
                runner=execution.runner,
                sleep=execution.sleep,
                progress=False,
            )
        except sim_batch.BatchPollTimeout as exc:
            evidence["remote"] = {"job_id": job_id}
            return _fail(FAIL_POLL_TIMEOUT, f"{exc}", evidence)
        except sim_batch.BatchError as exc:
            evidence["remote"] = {"job_id": job_id}
            return _fail(FAIL_SUBMIT, f"{exc}", evidence)

        evidence["remote"] = sim_batch._batch_environment(config, job_id, status)
        collected = os.path.join(scratch, "outputs")
        try:
            sim_batch.collect_outputs(
                config, job_id, collected, runner=execution.runner
            )
        except sim_batch.BatchError as exc:
            return _fail(FAIL_COLLECT, f"collecting outputs failed: {exc}", evidence)
        evidence["remote"].update(sim_batch._read_runner_identity(collected))

        state = status.get("state")
        exit_code = sim_batch._status_exit_code(status)
        if state != "done" or exit_code not in (0, None):
            rejection = _runner_rejection(collected)
            if rejection is not None:
                return _fail(FAIL_RUNNER, rejection, evidence)
            failure = FAIL_JOB_TIMEOUT if state == "timeout" else FAIL_JOB_FAILED
            return _fail(
                failure,
                f"batch job {job_id} finished {state!r} (exit {exit_code})",
                evidence,
            )

        return _compare(collected, committed_hash, job_id, evidence)


def _compare(
    collected: str, committed_hash: str, job_id: str, evidence: dict[str, Any]
) -> GeneratorOutcome:
    try:
        with open(os.path.join(collected, OUTPUT_FILENAME), "rb") as handle:
            data = handle.read()
    except OSError:
        return _fail(
            FAIL_OUTPUT_MISSING,
            f"batch job {job_id} finished without collected generator output",
            evidence,
        )
    output_hash = _sha256_bytes(data)
    try:
        with open(
            os.path.join(collected, OUTPUT_HASH_FILENAME), encoding="utf-8"
        ) as handle:
            reported = handle.read().strip()
    except OSError:
        reported = ""
    if reported != output_hash or not data:
        return _fail(
            FAIL_OUTPUT_INCOMPLETE,
            f"batch job {job_id}: collected output does not match the hash the "
            "runner reported (or is empty)",
            evidence,
        )
    evidence["output_hash"] = output_hash
    if output_hash == committed_hash:
        return GeneratorOutcome(
            STATE_FRESH, evidence={**evidence, "executed_by_klt": True}
        )
    return GeneratorOutcome(
        STATE_STALE,
        message="generator output differs from the committed artifact",
        evidence={**evidence, "executed_by_klt": True},
    )


__all__ = [
    "APPROVALS_ENV",
    "GeneratorApproval",
    "GeneratorApprovalError",
    "GeneratorExecution",
    "GeneratorOutcome",
    "build_job_spec",
    "load_approvals",
    "match_approval",
    "resolve_execution",
    "run_approved_generator",
]
