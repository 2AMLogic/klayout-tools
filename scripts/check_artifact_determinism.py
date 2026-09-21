#!/usr/bin/env python3
"""Regenerate the golden artifacts twice -- varied hash seed, varied checkout
path -- and byte-compare the results (issue #2225).

Two nondeterminism classes ship past a suite that only ever regenerates an
artifact **once**, under one hash seed, from one checkout:

  1. **set/dict-iteration-dependent ordering.** `PYTHONHASHSEED` only defaults
     to random when it is unset, and a CI runner that effectively fixes the
     environment reproduces one ordering forever. A count-tied list emitted in
     set-iteration order then byte-compares clean on every run until somebody
     regenerates on a machine that hashes differently.
  2. **host-absolute paths baked into a committed artifact.** A generator that
     records `str(some_path)` instead of a repo-relative path byte-compares
     clean on the machine that produced it and nowhere else.

Both were caught downstream (2AMLogic/gf180-surge PR #39, SXT-013) only after
an environment happened to differ. This check makes the environment differ on
purpose, every run: it regenerates the same artifacts from **two checkouts at
different filesystem paths** under **two different explicit hash seeds**, then
byte-compares. An artifact that depends on either variable differs, and the
check names it.

It is deliberately *not* a pytest test: the whole point is two checkouts at
two paths, which is a property of the job, not of the process. `ci.yml`'s
`Golden artifacts (hash seed + path varied)` job supplies them.

Usage:

    python3 scripts/check_artifact_determinism.py \
        --checkout primary \
        --checkout alt/second-checkout \
        [--seed 12345 --seed 67890] [--format json] [--annotate]

Exit codes (mirroring `scripts/check_ci_wall_clock.py`'s tiering):

  0  every regenerated artifact was byte-identical across both runs
  1  at least one artifact differed, or embeds a host-absolute path
  2  the check itself could not run (a generator failed, fewer than two
     distinct checkouts, an unreadable manifest) -- a check that silently
     stops checking is worse than no check at all, so this is never a green.

Everything here is standard library: the generators themselves need the
project's dependencies, but this driver must not add a second dependency
surface to a job whose whole job is comparing bytes.
"""

from __future__ import annotations

import argparse
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
#: Keeping the subset small and fast is a requirement, not an accident --
#: see docs/guides/ci-wall-clock-budget.md. All three together run in ~1.5 s.
DEFAULT_GENERATORS: tuple[dict[str, object], ...] = (
    {
        "script": "tests/golden_deck/generate_golden_deck.py",
        "artifacts": ["tests/golden_deck/*/manifest.json"],
    },
    {
        "script": "tests/corpus/generate_golden.py",
        "artifacts": ["tests/corpus/golden/*/*.layers.json"],
    },
    {
        "script": "tests/golden_metrics/generate_golden_metrics.py",
        "artifacts": ["tests/golden_metrics/*.json"],
    },
)

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
    script: str
    artifacts: tuple[str, ...]


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
        generators.append(
            Generator(
                script=str(entry["script"]), artifacts=tuple(str(g) for g in globs)
            )
        )
    return generators


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
    """Regenerate every artifact in one checkout under one hash seed."""
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
    # rather than silently dropping out of coverage.
    relpaths = _declared(checkout, generators) | (_git_dirty(checkout) - before_dirty)
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


def render_text(runs: list[Run], findings: list[Finding]) -> str:
    lines = ["Golden-artifact determinism check (hash seed + checkout path varied)"]
    for index, run in enumerate(runs, start=1):
        lines.append(f"  run {index}: {run.label}")
    lines.append(f"  artifacts compared: {len(runs[0].artifacts)}")
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


def render_json(runs: list[Run], findings: list[Finding]) -> str:
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
        if not runs[0].artifacts:
            raise CannotRun(
                "no artifacts were collected -- the generators wrote nothing this "
                "check knows about, so it would have passed without checking anything"
            )
        findings = compare_runs(runs)
    except CannotRun as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    text = render_text(runs, findings)
    print(render_json(runs, findings) if args.format == "json" else text)
    if args.annotate:
        for annotation in render_annotations(findings):
            print(annotation)
    write_step_summary(text)
    return EXIT_DIFFER if findings else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
