#!/usr/bin/env python3
"""Regenerate the golden artifacts twice -- varied hash seed, varied checkout
path -- and byte-compare the results (issue #2225; float provenance in
issue #2279).

Three nondeterminism classes ship past a suite that only ever regenerates an
artifact **once**, under one hash seed, from one checkout:

  1. **set/dict-iteration-dependent ordering.** `PYTHONHASHSEED` only defaults
     to random when it is unset, and a CI runner that effectively fixes the
     environment reproduces one ordering forever. A count-tied list emitted in
     set-iteration order then byte-compares clean on every run until somebody
     regenerates on a machine that hashes differently.
  2. **host-absolute paths baked into a committed artifact.** A generator that
     records `str(some_path)` instead of a repo-relative path byte-compares
     clean on the machine that produced it and nowhere else.
  3. **host-libm float provenance (issue #2279).** A generator that computes a
     committed value through a transcendental -- `math.pow`, `math.exp`,
     `**` on a float, numpy `power`/`float_power` -- emits bytes whose last
     ulp is *allowed* to differ between conforming libms. gf180-surge's RTL
     coefficients diverged exactly this way across macOS py3.14 and ubuntu
     py3.12 (PR #49), and `np.float32` power diverged again from
     double-pow-then-round (~0.6% of values, NEP-50 float32 contagion;
     PR #45). Both runs of this check sit on one host, so the divergence is
     invisible to the byte-compare by construction -- class 3 is caught
     instead by a static scan of every declared generator script, below.

Classes 1 and 2 were caught downstream (2AMLogic/gf180-surge PR #39, SXT-013)
only after an environment happened to differ. This check makes the environment
differ on purpose, every run: it regenerates the same artifacts from **two
checkouts at different filesystem paths** under **two different explicit hash
seeds**, then byte-compares. An artifact that depends on either variable
differs, and the check names it.

**Float provenance (issue #2279).** Every manifest generator declares a
`float_discipline`:

  - `integer-exact` (default) -- only integer/exact arithmetic reaches the
    committed bytes. The check statically scans the generator script for
    host-libm float computation (`math.pow`/`exp`/`log`/..., `cmath`, numpy
    transcendentals, `from math import pow` + bare `pow(...)`, the builtin
    two-argument `pow` with a float literal, `**` with a float-literal
    operand) and flags every hit as a `host_float_op` finding. A float
    *literal* is never flagged: parsing `0.1` to the nearest double is
    correctly rounded, so a committed literal table is byte-stable on every
    host. Nor are the operations IEEE 754 defines exactly or requires to be
    correctly rounded (`+ - * / %`, `math.sqrt`, `math.fsum`, `math.fma`,
    `math.ldexp`, ...).
  - `pinned-table` -- the floats come from a committed literal table in the
    source. The scan is identical, because *computing* over table values is
    the same hazard; the discipline's value is the promise it records (the
    numbers must not be recomputed at all) and the remedy it suggests.
  - `pinned-artifact` -- the escape hatch: the derived artifact is committed
    once and pinned, because regeneration is not trusted across hosts (the
    gf180-surge `cfg.hex` shape). The entry records `generator_sha256`,
    `regenerate`, and per-artifact `artifact_sha256`; the check **never
    regenerates a pinned generator** -- drift detection compares against the
    pin, not silent regeneration -- and verifies instead that the generator
    source still hashes to `generator_sha256` (`stale_pin` otherwise) and
    that the committed bytes still hash to `artifact_sha256` (`pin_drift`
    otherwise). A JSON artifact's envelope mirrors the pin as
    `"pinned": true` plus `generator`/`generator_sha256`/`regenerate`
    (docs/json-contract.md, "Pinned derived artifacts").

The scan's boundary is the generator **script** -- not the library modules it
imports. Floats in analysis code are legitimate (issue #2279's stated
non-goal); only the committed-byte path is held to the rule.

It is deliberately *not* a pytest test: the whole point is two checkouts at
two paths, which is a property of the job, not of the process. `ci.yml`'s
`Golden artifacts (hash seed + path varied)` job supplies them.

Usage:

    python3 scripts/check_artifact_determinism.py \
        --checkout primary \
        --checkout alt/second-checkout \
        [--seed 12345 --seed 67890] [--format json] [--annotate]

Exit codes (mirroring `scripts/check_ci_wall_clock.py`'s tiering):

  0  every regenerated artifact was byte-identical across both runs, no
     generator carries an undeclared host-libm float path, and every pin
     verified
  1  at least one artifact differed, embeds a host-absolute path, computes
     through host libm without a pinned-artifact declaration, or carries a
     stale/drifted pin
  2  the check itself could not run (a generator failed, fewer than two
     distinct checkouts, an unreadable or malformed manifest) -- a check that
     silently stops checking is worse than no check at all, so this is never
     a green.

Everything here is standard library: the generators themselves need the
project's dependencies, but this driver must not add a second dependency
surface to a job whose whole job is comparing bytes.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_DIFFER = 1
EXIT_CANNOT_RUN = 2

#: The representative artifact-generating subset this check drives. Every
#: entry is an existing, deterministic-by-design generator (each one's own
#: module docstring says so) plus the repo-relative globs it writes; the
#: globs are the *declared* artifact set, which the git-dirty sweep below
#: widens automatically if a generator ever writes somewhere undeclared.
#:
#: Each entry also declares its float discipline (issue #2279): all three of
#: these generators reach their committed bytes through integer/exact
#: arithmetic only -- dbu-level box math, byte counts and layer tables --
#: which the static scan below re-verifies on every run. `integer-exact` is
#: also the default for manifest entries that omit the field; spelling it out
#: here makes the shipped manifest read as the worked example it is.
#:
#: Keeping the subset small and fast is a requirement, not an accident --
#: see docs/guides/ci-wall-clock-budget.md. All three together run in ~1.5 s.
DEFAULT_GENERATORS: tuple[dict[str, object], ...] = (
    {
        "script": "tests/golden_deck/generate_golden_deck.py",
        "artifacts": ["tests/golden_deck/*/manifest.json"],
        "float_discipline": "integer-exact",
    },
    {
        "script": "tests/corpus/generate_golden.py",
        "artifacts": ["tests/corpus/golden/*/*.layers.json"],
        "float_discipline": "integer-exact",
    },
    {
        "script": "tests/golden_metrics/generate_golden_metrics.py",
        "artifacts": ["tests/golden_metrics/*.json"],
        "float_discipline": "integer-exact",
    },
)

#: The float-discipline vocabulary a manifest entry may declare (issue
#: #2279). See the module docstring for what each promises.
FLOAT_DISCIPLINES: tuple[str, ...] = (
    "integer-exact",
    "pinned-table",
    "pinned-artifact",
)

#: Discipline assumed for a manifest entry that declares none. The safe
#: default: an unannotated generator is held to the strictest scanning, and
#: loosening it is a deliberate act recorded in the manifest.
DEFAULT_FLOAT_DISCIPLINE = "integer-exact"

#: The escape-hatch discipline: its generators are never regenerated (drift
#: detection compares against the pin, not silent regeneration).
PINNED_ARTIFACT = "pinned-artifact"

#: Manifest pin fields, required (and only permitted) on a `pinned-artifact`
#: entry. `artifact_sha256` maps each pinned artifact's repo-relative path to
#: the sha256 of its committed bytes.
_PIN_FIELDS = ("generator_sha256", "regenerate", "artifact_sha256")

_HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")

#: What a reader of a `host_float_op` finding should do instead -- emitted
#: once per finding so the JSON payload carries the remedy, not just the
#: complaint (mirrors env_provenance.ENVELOPE_LINT_RECOMMENDATION's role for
#: the path lint, issue #2224).
FLOAT_PROVENANCE_RECOMMENDATION = (
    "the result's last ulp is allowed to differ between conforming libms, so "
    "the committed bytes can diverge across interpreters/platforms even "
    "though every run on this host byte-compares clean (issue #2279). Make "
    "the path integer-exact, read the values from a committed pinned table "
    "(a decimal literal parses to the same double on every host), or declare "
    'float_discipline "pinned-artifact" with generator_sha256 + regenerate '
    "+ artifact_sha256 -- see docs/guides/golden-artifact-determinism.md, "
    "'float provenance rules for committed evidence'."
)

# --------------------------------------------------------------------------
# Host-libm float computation: what the static scan flags (issue #2279)
# --------------------------------------------------------------------------

#: Per-module callables whose floating-point result is permitted to vary in
#: the last ulp between conforming libms -- the transcendental set, plus
#: `hypot`/`dist`/`cbrt`, which were famously double-rounded or unspecified
#: across libm versions (glibc only correctly-rounded `hypot` this century).
#: Anything reached through these cannot be trusted to byte-compare across
#: interpreters/platforms.
_HOST_FLOAT_CALLS: dict[str, frozenset[str]] = {
    "math": frozenset(
        {
            "acos",
            "acosh",
            "asin",
            "asinh",
            "atan",
            "atan2",
            "atanh",
            "cbrt",
            "cos",
            "cosh",
            "dist",
            "erf",
            "erfc",
            "exp",
            "exp2",
            "expm1",
            "gamma",
            "hypot",
            "lgamma",
            "log",
            "log10",
            "log1p",
            "log2",
            "pow",
            "sin",
            "sinh",
            "tan",
            "tanh",
        }
    ),
    # Complex functions are algorithmically defined, not required-correctly-
    # rounded, so even `cmath.sqrt` is a host-libm dependence.
    "cmath": frozenset(
        {
            "acos",
            "acosh",
            "asin",
            "asinh",
            "atan",
            "atanh",
            "cos",
            "cosh",
            "exp",
            "log",
            "log10",
            "phase",
            "polar",
            "pow",
            "rect",
            "sin",
            "sinh",
            "sqrt",
            "tan",
            "tanh",
        }
    ),
    # numpy routes elementwise transcendentals through the host libm or its
    # own SIMD kernels -- and `np.float32` power diverges from
    # double-pow-then-round besides (gf180-surge PR #45), which is the exact
    # incident class. `np.sqrt` is excluded: it is the hardware square root,
    # correctly rounded for both float32 and float64.
    "numpy": frozenset(
        {
            "arccos",
            "arccosh",
            "arcsin",
            "arcsinh",
            "arctan",
            "arctan2",
            "arctanh",
            "cbrt",
            "cos",
            "cosh",
            "exp",
            "exp2",
            "expm1",
            "float_power",
            "log",
            "log10",
            "log1p",
            "log2",
            "power",
            "sin",
            "sinh",
            "tan",
            "tanh",
        }
    ),
}


def _flagged_calls(module: str) -> frozenset[str]:
    return _HOST_FLOAT_CALLS.get(module, frozenset())


#: Absolute-path shapes that must never appear inside a committed artifact.
#: A path under the *running* checkout is caught by the byte-compare anyway
#: (the two checkouts sit at different paths); this catches the other half --
#: a foreign host's path committed from somebody's laptop, which byte-compares
#: clean on every runner because nothing regenerates it there.
_HOST_PATH_PATTERN = re.compile(
    rb"(?:/Users/|/home/|/root/|/private/var/folders/|/var/folders/"
    rb"|/tmp/|[A-Za-z]:\\{1,2}Users\\{1,2})"
)

#: Bounded diff excerpt in the text report -- enough to see *what* moved
#: (usually two swapped list entries), never the whole artifact.
_DIFF_EXCERPT_LINES = 12

_MAX_SEED = 2**32 - 1


class CannotRun(Exception):
    """The check could not be performed (exit 2, never a silent pass)."""


@dataclass(frozen=True)
class Generator:
    """One declared artifact generator (issue #2225), plus its float
    discipline and -- for `pinned-artifact` entries (issue #2279) -- the pin:
    the generator source hash and per-artifact content hashes recorded at pin
    time, and the command a human re-generates with. `artifact_sha256` is a
    tuple of (path, hash) pairs rather than a dict so the dataclass stays
    frozen/hashable; `pinned_files()` restores the mapping."""

    script: str
    artifacts: tuple[str, ...]
    float_discipline: str = DEFAULT_FLOAT_DISCIPLINE
    generator_sha256: str | None = None
    regenerate: str | None = None
    artifact_sha256: tuple[tuple[str, str], ...] = ()

    @property
    def pinned(self) -> bool:
        return self.float_discipline == PINNED_ARTIFACT

    def pinned_files(self) -> dict[str, str]:
        """The pinned artifact paths -> expected sha256 of the committed
        bytes (empty for a non-pinned entry)."""
        return dict(self.artifact_sha256)


@dataclass
class Run:
    """One full regeneration pass: one checkout, one hash seed."""

    checkout: Path
    seed: str
    artifacts: dict[str, bytes] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.checkout} (PYTHONHASHSEED={self.seed})"


@dataclass
class Finding:
    artifact: str
    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"artifact": self.artifact, "kind": self.kind, "detail": self.detail}


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def load_generators(manifest_path: Path | None) -> list[Generator]:
    if manifest_path is None:
        raw: object = {
            "schema_version": SCHEMA_VERSION,
            "generators": DEFAULT_GENERATORS,
        }
    else:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CannotRun(f"cannot read manifest {manifest_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise CannotRun("manifest must be a JSON object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CannotRun(
            f"manifest schema_version {version!r} is not supported "
            f"(this check speaks {SCHEMA_VERSION})"
        )
    entries = raw.get("generators")
    if not isinstance(entries, (list, tuple)) or not entries:
        raise CannotRun("manifest declares no generators")

    generators: list[Generator] = []
    for entry in entries:
        if not isinstance(entry, dict) or "script" not in entry:
            raise CannotRun(f"malformed generator entry: {entry!r}")
        globs = entry.get("artifacts") or []
        if not isinstance(globs, list):
            raise CannotRun(f"generator {entry['script']!r}: artifacts must be a list")

        # Float discipline (issue #2279). A typo'd discipline must be loud
        # (exit 2), never a silent fall-through to the default scan -- the
        # same rule `schema_version` above is held to.
        script_name = str(entry["script"])
        discipline = entry.get("float_discipline", DEFAULT_FLOAT_DISCIPLINE)
        if discipline not in FLOAT_DISCIPLINES:
            raise CannotRun(
                f"generator {script_name!r}: unknown float_discipline "
                f"{discipline!r} (expected one of {', '.join(FLOAT_DISCIPLINES)})"
            )

        # Pin fields: required on a pinned-artifact entry, a likely mistake
        # anywhere else -- a `generator_sha256` recorded against
        # `integer-exact` would look like a pin while nothing verified it.
        present = [name for name in _PIN_FIELDS if name in entry]
        if discipline == PINNED_ARTIFACT:
            missing = [name for name in _PIN_FIELDS if name not in entry]
            if missing:
                raise CannotRun(
                    f"generator {script_name!r}: float_discipline "
                    f"{PINNED_ARTIFACT!r} requires {', '.join(_PIN_FIELDS)} "
                    f"(missing: {', '.join(missing)})"
                )
            generator_sha256 = _validated_hash(entry, "generator_sha256", script_name)
            regenerate = entry["regenerate"]
            if not isinstance(regenerate, str) or not regenerate.strip():
                raise CannotRun(
                    f"generator {script_name!r}: regenerate must be a non-empty "
                    "command string -- it is the recorded human path to "
                    "regenerate (and re-pin) the artifact"
                )
            artifact_sha256 = _validated_artifact_hashes(entry, script_name)
        else:
            if present:
                raise CannotRun(
                    f"generator {script_name!r}: {', '.join(present)} only mean "
                    f"something with float_discipline {PINNED_ARTIFACT!r}; a pin "
                    "recorded against another discipline would never be verified"
                )
            generator_sha256, regenerate, artifact_sha256 = None, None, ()

        generators.append(
            Generator(
                script=script_name,
                artifacts=tuple(str(g) for g in globs),
                float_discipline=discipline,
                generator_sha256=generator_sha256,
                regenerate=regenerate,
                artifact_sha256=artifact_sha256,
            )
        )
    return generators


def _validated_hash(entry: dict[str, object], name: str, script_name: str) -> str:
    """A 64-hex sha256 recorded in a manifest entry, lower-cased -- a pin
    whose hash is malformed would otherwise fail to ever match, or worse,
    silently match nothing and look like drift."""
    value = entry[name]
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value.strip()):
        raise CannotRun(
            f"generator {script_name!r}: {name} must be a 64-hex sha256, got {value!r}"
        )
    return value.strip().lower()


def _validated_artifact_hashes(
    entry: dict[str, object], script_name: str
) -> tuple[tuple[str, str], ...]:
    """The `artifact_sha256` pin mapping, validated and normalised to sorted
    (path, hash) pairs. Empty is refused: a pinned entry that pins no bytes
    pins nothing and verifies nothing."""
    mapping = entry["artifact_sha256"]
    if not isinstance(mapping, dict) or not mapping:
        raise CannotRun(
            f"generator {script_name!r}: artifact_sha256 must be a non-empty "
            "mapping of repo-relative artifact path to 64-hex sha256"
        )
    pairs: list[tuple[str, str]] = []
    for path, digest in mapping.items():
        if not isinstance(path, str) or not path:
            raise CannotRun(
                f"generator {script_name!r}: artifact_sha256 keys must be "
                f"repo-relative paths, got {path!r}"
            )
        if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest.strip()):
            raise CannotRun(
                f"generator {script_name!r}: artifact_sha256[{path!r}] must be "
                f"a 64-hex sha256, got {digest!r}"
            )
        pairs.append((path, digest.strip().lower()))
    return tuple(sorted(pairs))


# --------------------------------------------------------------------------
# Float provenance: the static scan + pin verification (issue #2279)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FloatOp:
    """One host-libm float computation found in a generator script."""

    line: int
    construct: str
    source_line: str


def _has_float_literal(node: ast.expr) -> bool:
    """Whether any `ast.Constant` in `node`'s subtree is a float literal.

    This is the scan's deliberate asymmetry: a *literal* like `0.1` is
    host-independent (str->double parsing is correctly rounded by IEEE 754
    and Python guarantees repr round-tripping), so literals alone are never
    flagged -- but a literal *operand* to `**` (or to builtin `pow`) means
    the operation goes through libm power and its last ulp is host's.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, float):
            return True
    return False


def scan_float_ops(source: str, filename: str = "<generator>") -> list[FloatOp]:
    """Every host-libm float computation in `source`, in first-appearance
    order (empty list when clean).

    Four construct classes are flagged, all for the same reason -- the
    result's last ulp is permitted to vary between conforming libms:

    - an attribute call into `math`/`cmath`/`numpy` naming one of
      :data:`_HOST_FLOAT_CALLS`' functions (`math.pow(1.001, n)`,
      `np.float_power(a, b)`, ...);
    - a bare call to a name imported from one of those modules
      (`from math import pow` + `pow(...)`), and -- because `from math
      import *` hides the provenance -- a bare call to a flagged name when
      the module was star-imported;
    - the builtin two-argument `pow(a, b)` when either argument contains a
      float literal (three-argument `pow(a, b, m)` is modular *integer*
      arithmetic and never flagged);
    - `**` / `**=` with a float literal on either side -- on CPython, float
      `**` *is* libm `pow` (the gf180-surge PR #49 incident's exact shape).

    Deliberately not flagged: `+ - * / %` and comparison (IEEE-exact or
    correctly rounded by the standard), `math.sqrt`/`fabs`/`fmod`/`fsum`/
    `fma`/`ldexp`-family (same), float literals (pinned tables are the
    remedy, and they are safe -- see `_has_float_literal`), and float
    *formatting* (shortest-repr and fixed-precision rendering are correctly
    rounded, so the same double prints the same everywhere).

    The scan is a source-level screen, not a proof: it cannot see through a
    name to a value's dtype, so `arr ** 2` on a `float32` array goes
    unflagged. That residual risk is exactly what the `pinned-artifact`
    discipline is for -- declare it and pin the bytes.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise CannotRun(f"cannot parse {filename}: {exc}") from exc

    # Pass 1: resolve the local aliases that refer to a flagged module
    # (`import numpy as np` -> "np"), the bare names bound to one of its
    # flagged callables (`from math import pow as p` -> "p"), and any
    # star-import (whose bindings are unknowable statically).
    module_aliases: dict[str, str] = {}
    bare_flagged: dict[str, str] = {}
    star_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _HOST_FLOAT_CALLS:
                    module_aliases[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root not in _HOST_FLOAT_CALLS:
                continue
            if any(alias.name == "*" for alias in node.names):
                star_imports.add(root)
                continue
            for alias in node.names:
                if alias.name in _flagged_calls(root):
                    bare_flagged[alias.asname or alias.name] = root

    # Pass 2: find the constructs.
    lines = source.splitlines()
    ops: list[FloatOp] = []

    def record(node: ast.AST, construct: str) -> None:
        line_no = getattr(node, "lineno", 0)
        source_line = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
        ops.append(FloatOp(line=line_no, construct=construct, source_line=source_line))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                module = module_aliases.get(func.value.id)
                if module and func.attr in _flagged_calls(module):
                    record(node, f"{func.value.id}.{func.attr}(...)")
            elif isinstance(func, ast.Name):
                name = func.id
                if name in bare_flagged:
                    origin = bare_flagged[name]
                    record(node, f"{name}(...) (imported from {origin})")
                elif any(name in _flagged_calls(module) for module in star_imports):
                    # `from math import *`: the name's provenance is
                    # unknowable statically, so a bare call to a flagged name
                    # is attributed to the star import rather than missed.
                    record(node, f"{name}(...) (star-imported float module)")
                elif (
                    name == "pow"
                    and name not in bare_flagged
                    and len(node.args) == 2
                    and not node.keywords
                    and (
                        _has_float_literal(node.args[0])
                        or _has_float_literal(node.args[1])
                    )
                ):
                    record(node, "pow(a, b) with a float-literal argument")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if _has_float_literal(node.left) or _has_float_literal(node.right):
                record(node, "`**` with a float-literal operand")
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Pow):
            if _has_float_literal(node.value):
                record(node, "`**=` with a float-literal operand")
    return ops


def _sha256_hex(payload: bytes) -> str:
    """Full sha256 hex digest -- the pin's currency. (`_sha` above is the
    *report* width, truncated for display; a pin recorded at 16 hex would
    invite collisions-by-typo, so pins always compare in full.)"""
    return hashlib.sha256(payload).hexdigest()


def _host_float_finding(generator: Generator, op: FloatOp) -> Finding:
    detail = (
        f"host-libm float computation in a committed-artifact generator: "
        f"{op.construct} at {generator.script}:{op.line}\n"
        f"      {op.source_line}\n"
        f"      {FLOAT_PROVENANCE_RECOMMENDATION}"
    )
    return Finding(artifact=generator.script, kind="host_float_op", detail=detail)


def _pin_findings(checkout: Path, generator: Generator) -> list[Finding]:
    """Verify a pinned-artifact entry against its commit (issue #2279).

    Two questions, both static, both against the bytes as committed:

    - does the generator source still hash to `generator_sha256`? If not,
      the committed artifacts can no longer be attributed to the committed
      generator -- `stale_pin`.
    - does each pinned file still exist and hash to its `artifact_sha256`?
      If not, the committed bytes moved without a re-pin -- `pin_drift`.

    Regeneration is deliberately *not* attempted: the pin exists precisely
    because regeneration is not trusted across hosts.
    """
    findings: list[Finding] = []

    script_path = checkout / generator.script
    actual_generator = _sha256_hex(script_path.read_bytes())
    assert generator.generator_sha256 is not None  # manifest-validated
    if actual_generator != generator.generator_sha256:
        findings.append(
            Finding(
                artifact=generator.script,
                kind="stale_pin",
                detail=(
                    f"the pin records generator_sha256 "
                    f"{generator.generator_sha256[:16]}..., but "
                    f"{generator.script} now hashes to "
                    f"{actual_generator[:16]}...: the committed artifacts can "
                    "no longer be attributed to the committed generator. "
                    "Regenerate on a trusted host, review the diff, and "
                    "re-pin (update generator_sha256/artifact_sha256 in the "
                    "same commit as the regenerated bytes), or restore the "
                    "generator. See docs/guides/golden-artifact-determinism.md."
                ),
            )
        )

    for rel, expected in generator.artifact_sha256:
        path = checkout / rel
        if not path.is_file():
            findings.append(
                Finding(
                    artifact=rel,
                    kind="pin_drift",
                    detail=(
                        f"the pin records this artifact, but it is absent from "
                        f"{checkout}: a pinned artifact must stay committed -- "
                        "restore the bytes or re-pin after a reviewed "
                        "regeneration"
                    ),
                )
            )
            continue
        actual = _sha256_hex(path.read_bytes())
        if actual != expected:
            findings.append(
                Finding(
                    artifact=rel,
                    kind="pin_drift",
                    detail=(
                        f"the pin records sha256 {expected[:16]}..., but the "
                        f"committed bytes hash to {actual[:16]}...: the "
                        "artifact moved without a re-pin. Restore the pinned "
                        "bytes, or re-pin (update artifact_sha256 in the same "
                        "commit as the regenerated bytes, per "
                        "docs/guides/golden-artifact-determinism.md)."
                    ),
                )
            )
    return findings


def static_findings(checkout: Path, generators: list[Generator]) -> list[Finding]:
    """The checkout-independent findings: the float-provenance scan for every
    non-pinned generator and the pin verification for every pinned one.

    These run against *both* checkouts (they are the same commit, but each
    copy is scanned, and the results deduped, so a checkout-specific
    corruption cannot slip between the cracks).
    """
    findings: list[Finding] = []
    for generator in generators:
        script_path = checkout / generator.script
        if not script_path.is_file():
            raise CannotRun(f"generator {generator.script} not found under {checkout}")
        if generator.pinned:
            findings.extend(_pin_findings(checkout, generator))
            continue
        source = script_path.read_text(encoding="utf-8")
        for op in scan_float_ops(source, generator.script):
            findings.append(_host_float_finding(generator, op))
    return findings


def _pinned_artifact_paths(generators: list[Generator]) -> set[str]:
    """Repo-relative paths pinned by any `pinned-artifact` entry. These are
    excluded from the two-run byte-compare and from the dirty sweep: their
    guarantee is integrity-of-the-commit (the pin), not regenerability."""
    paths: set[str] = set()
    for generator in generators:
        if generator.pinned:
            paths.update(generator.pinned_files())
    return paths


# --------------------------------------------------------------------------
# Running one pass
# --------------------------------------------------------------------------


def _git_dirty(checkout: Path) -> set[str]:
    """Repo-relative paths git reports as modified/untracked, or an empty set
    when `checkout` is not a git worktree (synthetic fixtures, tarball
    checkouts). Gitignored files never appear, so `.venv`/`__pycache__` churn
    is invisible here by construction."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    if proc.returncode != 0:
        return set()
    dirty: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # Rename entries are "old -> new"; the new path is the artifact.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty.add(path.strip('"'))
    return dirty


def _collect(checkout: Path, relpaths: set[str]) -> dict[str, bytes]:
    collected: dict[str, bytes] = {}
    for rel in sorted(relpaths):
        path = checkout / rel
        if path.is_file():
            collected[rel] = path.read_bytes()
    return collected


def _declared(checkout: Path, generators: list[Generator]) -> set[str]:
    found: set[str] = set()
    for generator in generators:
        for pattern in generator.artifacts:
            for path in sorted(checkout.glob(pattern)):
                if path.is_file():
                    found.add(path.relative_to(checkout).as_posix())
    return found


def execute_run(
    checkout: Path,
    seed: str,
    generators: list[Generator],
    *,
    python: str,
    timeout: int,
    verbose: bool = True,
) -> Run:
    """Regenerate every artifact in one checkout under one hash seed.

    `pinned-artifact` generators are skipped on purpose (issue #2279): their
    bytes are the reference, regeneration is not trusted across hosts, and
    re-running them would overwrite the very commit the pin vouches for. The
    pin is verified statically instead (`static_findings`).
    """
    if not checkout.is_dir():
        raise CannotRun(f"checkout {checkout} does not exist")

    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    # The generators each `sys.path.insert(0, <their own repo>/src)`, but be
    # explicit: a second checkout must never import the first one's package
    # through an editable install that happens to be on the interpreter's path.
    src = checkout / "src"
    if src.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(
            [str(src)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )

    before_dirty = _git_dirty(checkout)

    for generator in generators:
        script = checkout / generator.script
        if not script.is_file():
            raise CannotRun(f"generator {generator.script} not found under {checkout}")
        if generator.pinned:
            if verbose:
                print(
                    f"  -> {generator.script} "
                    f"(pinned-artifact: not regenerated, pin verified against "
                    f"the commit)",
                    flush=True,
                )
            continue
        if verbose:
            print(f"  -> {generator.script} (PYTHONHASHSEED={seed})", flush=True)
        try:
            proc = subprocess.run(
                [python, str(script)],
                cwd=str(checkout),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CannotRun(
                f"generator {generator.script} timed out after {timeout}s in {checkout}"
            ) from exc
        except OSError as exc:
            raise CannotRun(f"cannot run {generator.script}: {exc}") from exc
        if proc.returncode != 0:
            raise CannotRun(
                f"generator {generator.script} failed in {checkout} "
                f"(exit {proc.returncode}):\n{proc.stdout}{proc.stderr}"
            )

    # Declared globs, widened by anything the run newly dirtied: a generator
    # that starts writing an artifact nobody declared is still compared,
    # rather than silently dropping out of coverage. Pinned artifacts are
    # subtracted: nothing regenerated them, and their guarantee is the pin.
    regenerating = [g for g in generators if not g.pinned]
    relpaths = _declared(checkout, regenerating) | (
        (_git_dirty(checkout) - before_dirty) - _pinned_artifact_paths(generators)
    )
    return Run(checkout=checkout, seed=seed, artifacts=_collect(checkout, relpaths))


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:16]


def _text_or_none(payload: bytes) -> list[str] | None:
    try:
        return payload.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _diff_excerpt(reference: bytes, other: bytes) -> str:
    left, right = _text_or_none(reference), _text_or_none(other)
    if left is None or right is None:
        return f"(binary artifact: {len(reference)} vs {len(other)} bytes)"
    lines = list(
        difflib.unified_diff(
            left, right, fromfile="run 1", tofile="run 2", lineterm="", n=0
        )
    )
    if not lines:
        return "(identical text, differing bytes -- line endings or trailing newline)"
    excerpt = lines[:_DIFF_EXCERPT_LINES]
    if len(lines) > _DIFF_EXCERPT_LINES:
        excerpt.append(f"... ({len(lines) - _DIFF_EXCERPT_LINES} more diff lines)")
    return "\n".join(excerpt)


def _embedded_checkout_path(payload: bytes, run: Run) -> bool:
    return str(run.checkout.resolve()).encode() in payload


def compare_runs(runs: list[Run]) -> list[Finding]:
    """Byte-compare every artifact across every run, and scan each for a
    host-absolute path. Findings name the artifact by repo-relative path."""
    reference = runs[0]
    findings: list[Finding] = []

    every_artifact: set[str] = set()
    for run in runs:
        every_artifact |= set(run.artifacts)

    for artifact in sorted(every_artifact):
        present = [run for run in runs if artifact in run.artifacts]
        if len(present) != len(runs):
            missing = [
                str(run.checkout) for run in runs if artifact not in run.artifacts
            ]
            findings.append(
                Finding(
                    artifact=artifact,
                    kind="missing",
                    detail=(
                        "regenerated in "
                        + ", ".join(str(r.checkout) for r in present)
                        + " but absent from "
                        + ", ".join(missing)
                    ),
                )
            )
            continue

        baseline = reference.artifacts[artifact]
        for run in runs[1:]:
            payload = run.artifacts[artifact]
            if payload == baseline:
                continue
            path_dependent = _embedded_checkout_path(
                baseline, reference
            ) or _embedded_checkout_path(payload, run)
            kind = "path_dependent" if path_dependent else "unstable_ordering"
            headline = (
                "regenerated bytes differ between checkouts, and the artifact "
                "embeds its own checkout's absolute path"
                if path_dependent
                else "regenerated bytes differ across hash seeds "
                "(set/dict-iteration ordering)"
            )
            detail = (
                f"{headline}\n"
                f"{reference.label}: sha256 {_sha(baseline)}\n"
                f"{run.label}: sha256 {_sha(payload)}\n"
                + _diff_excerpt(baseline, payload)
            )
            findings.append(Finding(artifact=artifact, kind=kind, detail=detail))

    # Host-absolute-path scan: independent of the byte-compare, because a
    # foreign host's path (committed from a laptop, never regenerated in CI)
    # is identical in both runs and so invisible to a comparison.
    for artifact in sorted(reference.artifacts):
        match = _HOST_PATH_PATTERN.search(reference.artifacts[artifact])
        if match is None:
            continue
        context = _host_path_context(reference.artifacts[artifact], match.start())
        findings.append(
            Finding(
                artifact=artifact,
                kind="host_path",
                detail=f"host-absolute path embedded in the artifact: {context}",
            )
        )
    return findings


def _host_path_context(payload: bytes, offset: int) -> str:
    start = max(0, offset - 20)
    snippet = payload[start : offset + 80]
    return snippet.decode("utf-8", errors="replace").replace("\n", " ")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render_text(
    runs: list[Run], findings: list[Finding], *, pinned_artifact_count: int = 0
) -> str:
    lines = ["Golden-artifact determinism check (hash seed + path varied)"]
    for index, run in enumerate(runs, start=1):
        lines.append(f"  run {index}: {run.label}")
    lines.append(f"  artifacts compared: {len(runs[0].artifacts)}")
    if pinned_artifact_count:
        lines.append(
            f"  pinned artifacts (pin-verified, not regenerated): "
            f"{pinned_artifact_count}"
        )
    lines.append("")

    if not findings:
        lines.append("OK: every regenerated artifact was byte-identical across runs.")
        return "\n".join(lines)

    lines.append(f"FAIL: {len(findings)} differing/suspect artifact(s):")
    for finding in findings:
        lines.append("")
        lines.append(f"  {finding.artifact}  [{finding.kind}]")
        for detail_line in finding.detail.splitlines():
            lines.append(f"      {detail_line}")
    lines.append("")
    lines.append("Differing artifact paths:")
    for artifact in sorted({f.artifact for f in findings}):
        lines.append(f"  {artifact}")
    return "\n".join(lines)


def render_json(
    runs: list[Run], findings: list[Finding], *, pinned_artifact_count: int = 0
) -> str:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "differ" if findings else "ok",
            "runs": [
                {
                    "checkout": str(run.checkout),
                    "seed": run.seed,
                    "artifacts": len(run.artifacts),
                }
                for run in runs
            ],
            "artifact_count": len(runs[0].artifacts),
            # Pinned artifacts sit outside the byte-compare (issue #2279);
            # reported so a consumer can see they were verified, not skipped.
            "pinned_artifact_count": pinned_artifact_count,
            "findings": [finding.as_dict() for finding in findings],
            "differing_artifacts": sorted({f.artifact for f in findings}),
        },
        indent=2,
    )


def render_annotations(findings: list[Finding]) -> list[str]:
    return [
        f"::error file={finding.artifact}::{finding.kind}: "
        f"{finding.detail.splitlines()[0] if finding.detail else 'differs across runs'}"
        for finding in findings
    ]


def write_step_summary(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("## Golden-artifact determinism\n\n```\n" + text + "\n```\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _validate_seed(seed: str) -> str:
    if seed == "random":
        return seed
    try:
        value = int(seed)
    except ValueError as exc:
        raise CannotRun(
            f"invalid --seed {seed!r}: expected an integer or 'random'"
        ) from exc
    if not 0 <= value <= _MAX_SEED:
        raise CannotRun(f"invalid --seed {seed!r}: outside 0..{_MAX_SEED}")
    return str(value)


def _resolve_seeds(requested: list[str], count: int) -> list[str]:
    if requested:
        if len(requested) != count:
            raise CannotRun(
                f"got {len(requested)} --seed value(s) for {count} checkout(s): "
                "pass one seed per checkout, or none to draw them randomly"
            )
        return [_validate_seed(seed) for seed in requested]
    # Distinct by construction: two runs that happen to share a seed would
    # silently degrade this into a path-only check.
    rng = random.SystemRandom()
    seeds: list[str] = []
    while len(seeds) < count:
        candidate = str(rng.randint(1, _MAX_SEED))
        if candidate not in seeds:
            seeds.append(candidate)
    return seeds


def _resolve_checkouts(raw: list[str], allow_same_path: bool) -> list[Path]:
    if len(raw) < 2:
        raise CannotRun(
            "need at least two --checkout paths: the point of this check is "
            "regenerating from two different filesystem paths"
        )
    checkouts = [Path(item).resolve() for item in raw]
    if not allow_same_path and len({str(c) for c in checkouts}) != len(checkouts):
        raise CannotRun(
            "the --checkout paths must be distinct: identical paths cannot "
            "detect path-dependence"
        )
    return checkouts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--checkout",
        action="append",
        default=[],
        metavar="PATH",
        help="a checkout to regenerate in (pass at least twice, at different paths)",
    )
    parser.add_argument(
        "--seed",
        action="append",
        default=[],
        metavar="N",
        help="PYTHONHASHSEED for the matching --checkout (integer or 'random'); "
        "defaults to distinct random seeds, echoed in the report",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSON manifest of generators/artifact globs "
        "(default: the built-in subset)",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="emit GitHub Actions ::error:: annotations",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter used to run each generator",
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="per-generator timeout, seconds"
    )
    parser.add_argument(
        "--allow-same-path",
        action="store_true",
        help="permit identical --checkout paths (seed-variation only; for tests)",
    )
    args = parser.parse_args(argv)

    try:
        checkouts = _resolve_checkouts(args.checkout, args.allow_same_path)
        seeds = _resolve_seeds(args.seed, len(checkouts))
        generators = load_generators(args.manifest)
        pinned_paths = _pinned_artifact_paths(generators)

        # Static phase first (issue #2279): the float scan and pin
        # verification need no regeneration, so a flagged generator fails
        # before anything runs. Both checkouts are scanned and the results
        # deduped -- same commit, but a checkout-specific corruption of one
        # copy must not hide behind the other's clean scan.
        static: dict[tuple[str, str, str], Finding] = {}
        for checkout in checkouts:
            for finding in static_findings(checkout, generators):
                static.setdefault(
                    (finding.artifact, finding.kind, finding.detail), finding
                )

        runs: list[Run] = []
        for checkout, seed in zip(checkouts, seeds, strict=True):
            if args.format == "text":
                print(f"regenerating in {checkout} (PYTHONHASHSEED={seed})", flush=True)
            runs.append(
                execute_run(
                    checkout,
                    seed,
                    generators,
                    python=args.python,
                    timeout=args.timeout,
                    verbose=args.format == "text",
                )
            )
        # A manifest of nothing-but-pinned entries legitimately collects zero
        # compared artifacts; anything else still needs at least one.
        if not runs[0].artifacts and not pinned_paths:
            raise CannotRun(
                "no artifacts were collected -- the generators wrote nothing this "
                "check knows about, so it would have passed without checking anything"
            )
        findings = list(static.values()) + compare_runs(runs)
    except CannotRun as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    pinned_count = sum(len(g.pinned_files()) for g in generators if g.pinned)
    text = render_text(runs, findings, pinned_artifact_count=pinned_count)
    print(
        render_json(runs, findings, pinned_artifact_count=pinned_count)
        if args.format == "json"
        else text
    )
    if args.annotate:
        for annotation in render_annotations(findings):
            print(annotation)
    write_step_summary(text)
    return EXIT_DIFFER if findings else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
