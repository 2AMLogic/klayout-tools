#!/usr/bin/env python3
"""Monotonic per-file line-size ratchet, checked against the merge-base
(issue #2766).

Agent PRs tend to grow files, and nothing stopped them:
`src/klayout_tools/extract.py` passed ~9.4k lines while the complexity
ratchet (#2034) only watched per-function cyclomatic complexity. This is the
missing per-FILE gate, and it is deliberately stricter than the complexity
ratchet in one key way: it is validated against the baseline read from the
**comparison revision** (the PR's merge-base, or a push-to-main's before
SHA), not from the working tree, so a PR can never loosen its own gate by
editing `file-size-baseline.json` (repo root).

Two baselines are inputs to every check:

1. The **trusted** baseline, read from the comparison revision. It alone
   establishes permitted paths, ceilings, and content-hash vendor
   exemptions.
2. The **proposed** baseline, read from the candidate tree (the working
   tree this checkout represents -- in CI, the PR merge result or the
   pushed main tip). It must exactly reflect the candidate's remaining
   debt: a surviving entry must equal the file's measured size, a file at
   or under the hard limit (or deleted) must have no entry, and every
   vendor hash must match the actual bytes of a candidate file.

Checking only candidate sizes against the old baseline would let stale
proposed ceilings through; trusting only the proposed baseline would let a
PR self-approve growth. Both sides are checked.

What fails (exit 1):

- a governed tracked file grows above its trusted ceiling;
- an unbaselined file exceeds the hard limit (new oversized file, or an
  old file that grew across the limit) -- unless the diff proves a
  non-growing rename from a trusted-baselined path;
- a proposed ceiling is stale: not exactly the file's current size;
- a proposed entry survives for a file that shrank to the limit or was
  deleted;
- a proposed debt entry or vendor hash is not present in the trusted
  baseline (a PR cannot mint new debt or new exemptions), subject to the
  narrow vendor-refresh exception below;
- a vendor hash entry that no longer matches a candidate file's actual
  bytes (changed exempt bytes cease to be exempt: the file falls under
  ordinary checking).

What never fails: a baselined file staying unchanged or shrinking (with
its proposed entry tightened to match, or removed once at/below the
limit), and the removal of debt entries or vendor hashes. Tightening is
always allowed; loosening never is.

Counting is physical lines: blank lines and comment lines count, and a
final line without a trailing newline counts as a line. An empty file is
0 lines. Governed source is tracked first-party Python (`.py`), Rust
(`.rs`), and shell (`.sh`), including tests, scripts, and examples; the
vendored agent-tooling trees listed in `EXCLUDED_PREFIXES` (the same ones
`pyproject.toml`'s `[tool.ruff]` exclude scopes ruff to code this repo
owns) are not governed, nor are docs, binary fixtures, or generated
reports (not source extensions).

Vendored code is identified by **content hash, never by directory name**:
a hash exemption in the trusted baseline covers a candidate file only when
the file's actual bytes hash to the recorded value. Moving first-party
code under a directory named `vendor/` exempts nothing; renaming a pinned
vendor file (path changes) or touching its bytes (hash changes) drops it
out of exemption and under ordinary checking.

The narrow vendor-refresh exception: when the candidate's entire diff
against the comparison revision touches only the baseline file itself and
paths already pinned in the trusted vendor inventory, replacing a pinned
entry's hash (same path, new reviewed upstream bytes) is permitted. A PR
that touches any first-party file alongside the vendor refresh keeps the
strict rule. This is the "reviewed upstream vendor updates update the
trusted inventory separately" path: it is mechanically scoped so it can
never authorize first-party growth or mint a pin for a file the trusted
baseline never pinned.

**Bootstrap** is the case where the comparison revision predates this
checker (no baseline file at that revision -- true of this gate's own
landing PR). The trusted baseline is then derived from the comparison
tree's actual measured content (every governed file over the hard limit
gets a ceiling equal to its measured size there), and the vendor inventory
starts empty: a bootstrap authorizes no hash exemptions. Brand-new
oversized files that only exist in the candidate (the landing PR's own
implementation, say) may enter the baseline at exactly their measured
landing size -- the landing diff is the review, and the must-equal rule
pins the ceiling to reality, so nothing beyond the visible diff is
authorized. What bootstrap forbids outright: raising any pre-existing
file's ceiling above its comparison-tree size, and minting vendor pins.
The proposed initial baseline therefore cannot simply authorize its own
entries; it passes only by exactly recording actual content.

Usage::

    # check the candidate tree against a comparison revision (CI's step;
    # normal mode is read-only)
    python scripts/check_file_size_baseline.py \
        --comparison-rev "$(git merge-base HEAD origin/main)"

    # regenerate the proposed baseline from the candidate tree after a
    # reduction (never adds exemptions; check mode still validates it)
    python scripts/check_file_size_baseline.py --update-baseline

Exit codes: 0 = pass, 1 = ratchet violations (real content failures),
2 = unusable input (missing/malformed baseline, unresolvable comparison
revision, git failure). CI fails on either non-zero code; the split keeps
"your PR grew a file" apart from "the gate could not run".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

# A governed tracked source file over this many physical lines carries a
# baseline entry; at or under it, it must not. Kept as a script constant
# (like check_complexity_baseline.py's MAX_COMPLEXITY) and recorded in the
# baseline JSON, where tests assert the two agree.
HARD_LIMIT = 500

BASELINE_SCHEMA_VERSION = 1
DEFAULT_BASELINE_NAME = "file-size-baseline.json"

# First-party source extensions the gate governs: Python, Rust, and shell,
# including tests, scripts, and examples. Binary fixtures, generated
# reports, and documentation are not source and need no line ceilings.
GOVERNED_EXTENSIONS = {".py", ".rs", ".sh"}

# Vendored agent-tooling trees this repo does not own and re-syncs from
# upstream -- the same exclusion rationale as pyproject.toml's
# [tool.ruff] `exclude`, which scopes ruff to code this repo owns. A
# directory *name* alone never establishes vendored status for anything
# else: exemption is content-hash only, and only via the trusted baseline.
EXCLUDED_PREFIXES = (".loom/", ".claude/", ".agents/")

BASELINE_RELATIVE = "scripts/check_file_size_baseline.py"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_UNUSABLE = 2

_FETCH_HINT = (
    "fetch it first, e.g.\n"
    "  git fetch --no-tags origin <revision>\n"
    "(CI fetches the PR merge-base / the push's before SHA itself; locally, "
    "compare with:\n"
    "  python scripts/check_file_size_baseline.py --comparison-rev "
    '"$(git merge-base HEAD origin/main)")'
)


class BaselineError(Exception):
    """Missing or malformed baseline data -- the gate cannot run (exit 2)."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class GitError(Exception):
    """A git read the gate depends on failed (exit 2)."""


# ---------------------------------------------------------------------------
# Line counting and the governed-file inventory
# ---------------------------------------------------------------------------


def count_lines(data: bytes) -> int:
    """Physical line count: blanks/comments count; a final unterminated
    line counts; an empty file is 0 lines."""
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def is_governed(path: str) -> bool:
    """True for tracked first-party Python/Rust/shell source paths (POSIX,
    repo-relative), outside the vendored agent-tooling trees."""
    if path.startswith(EXCLUDED_PREFIXES):
        return False
    return PurePosixPath(path).suffix in GOVERNED_EXTENSIONS


def _decode(raw: bytes) -> str:
    # os.fsdecode round-trips whatever byte sequence git reported (git
    # quotes nothing under -z), so filenames with spaces or non-UTF8
    # bytes survive to open()/JSON unchanged.
    return os.fsdecode(raw)


def _git_run(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
    )


def _git_out(repo_root: Path, *args: str) -> bytes:
    result = _git_run(repo_root, *args)
    if result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def resolve_revision(repo_root: Path, rev: str) -> str:
    """Fail with fetch instructions when the comparison ref is unavailable."""
    result = _git_run(
        repo_root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"
    )
    if result.returncode != 0:
        raise GitError(
            f"comparison revision {rev!r} not found in this repository -- "
            + _FETCH_HINT
        )
    return result.stdout.decode().strip()


def tracked_worktree_files(repo_root: Path) -> list[str]:
    """Governed tracked paths in the working tree, sorted (NUL-safe)."""
    out = _git_out(repo_root, "ls-files", "-s", "-z")
    paths = []
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode()
        if mode not in ("100644", "100755"):
            continue  # symlinks (120000) and submodules (160000)
        path = _decode(raw_path)
        if is_governed(path):
            paths.append(path)
    return sorted(paths)


def tracked_rev_files(repo_root: Path, rev: str) -> list[str]:
    """Governed tracked blob paths at a revision, sorted (NUL-safe)."""
    out = _git_out(repo_root, "ls-tree", "-r", "-z", rev)
    paths = []
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) < 2 or fields[1] != b"blob":
            continue  # trees, submodules
        if fields[0].decode() not in ("100644", "100755"):
            continue
        path = _decode(raw_path)
        if is_governed(path):
            paths.append(path)
    return sorted(paths)


def read_rev_blobs(repo_root: Path, rev: str, paths: list[str]) -> dict[str, bytes]:
    """Read many blobs from one revision via a single `git cat-file --batch`."""
    if not paths:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    blobs: dict[str, bytes] = {}
    try:
        for path in paths:
            request = f"{rev}:{path}\n".encode("utf-8", "surrogateescape")
            proc.stdin.write(request)
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) == 3 and header[2].isdigit():
                size = int(header[2])
                blobs[path] = proc.stdout.read(size)
                proc.stdout.read(1)  # trailing LF after the content
            # anything else ("<request> missing") cannot happen for a path
            # ls-tree just listed at this same immutable revision; skip it.
    finally:
        proc.stdin.close()
        proc.wait()
    return blobs


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    debts: dict[str, int]  # path -> line ceiling
    vendor_hashes: dict[str, str]  # sha256 -> recorded path


def _debt_path_problem(path: object) -> str | None:
    """Reason string when a debt entry's path is unusable, else None."""
    if not isinstance(path, str) or not path:
        return "must be a non-empty string"
    if path.startswith("/") or ".." in PurePosixPath(path).parts:
        return "must be relative to the repository root"
    return None


def _debt_lines_problem(lines: object) -> str | None:
    """Reason string when a debt entry's line count is unusable."""
    if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
        return f"must have a positive integer 'lines', got {lines!r}"
    return None


def _parse_debt_entry(
    entry: object, source: str, problems: list[str], debts: dict[str, int]
) -> None:
    if not isinstance(entry, dict) or set(entry) != {"path", "lines"}:
        problems.append(
            f"{source}: debt entry {entry!r} must have exactly 'path' and 'lines' keys"
        )
        return
    path = entry.get("path")
    path_problem = _debt_path_problem(path)
    if path_problem is not None:
        problems.append(f"{source}: debt entry path {path!r} {path_problem}")
        return
    lines_problem = _debt_lines_problem(entry.get("lines"))
    if lines_problem is not None:
        problems.append(f"{source}: debt entry for {path!r} {lines_problem}")
        return
    assert isinstance(path, str)
    if path in debts:
        problems.append(f"{source}: duplicate debt entry for {path!r}")
        return
    debts[path] = entry["lines"]


def _parse_debts(raw: object, source: str, problems: list[str]) -> dict[str, int]:
    if not isinstance(raw, list):
        problems.append(f"{source}: 'debts' must be a list")
        return {}
    debts: dict[str, int] = {}
    for entry in raw:
        _parse_debt_entry(entry, source, problems, debts)
    return debts


def _vendor_sha_problem(sha: object) -> str | None:
    """Reason string when a vendor entry's hash is unusable."""
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return "must be 64 lowercase hex digits"
    return None


def _parse_vendor_entry(
    entry: object, source: str, problems: list[str], vendor: dict[str, str]
) -> None:
    if not isinstance(entry, dict) or set(entry) != {"sha256", "path"}:
        problems.append(
            f"{source}: vendor entry {entry!r} must have exactly "
            "'sha256' and 'path' keys"
        )
        return
    sha = entry.get("sha256")
    sha_problem = _vendor_sha_problem(sha)
    if sha_problem is not None:
        problems.append(f"{source}: vendor entry sha256 {sha!r} {sha_problem}")
        return
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        problems.append(
            f"{source}: vendor entry for {str(sha)[:12]}... must record a "
            "non-empty 'path'"
        )
        return
    assert isinstance(sha, str)
    if sha in vendor:
        problems.append(f"{source}: duplicate vendor hash entry for {sha[:12]}...")
        return
    vendor[sha] = path


def _parse_vendor_hashes(
    raw: object, source: str, problems: list[str]
) -> dict[str, str]:
    if not isinstance(raw, list):
        problems.append(f"{source}: 'vendor_hashes' must be a list")
        return {}
    vendor: dict[str, str] = {}
    for entry in raw:
        _parse_vendor_entry(entry, source, problems, vendor)
    return vendor


def parse_baseline(data: bytes, source: str) -> Baseline:
    """Structurally validate baseline JSON; collect every problem before
    failing so one malformed file yields one actionable report."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError([f"{source}: not valid UTF-8 JSON: {exc}"]) from exc

    if not isinstance(doc, dict):
        raise BaselineError([f"{source}: top level must be a JSON object"])

    problems: list[str] = []
    if doc.get("schema_version") != BASELINE_SCHEMA_VERSION:
        problems.append(
            f"{source}: schema_version must be {BASELINE_SCHEMA_VERSION}, "
            f"got {doc.get('schema_version')!r}"
        )
    if doc.get("hard_limit") != HARD_LIMIT:
        problems.append(
            f"{source}: hard_limit must be {HARD_LIMIT} (what this checker "
            f"enforces), got {doc.get('hard_limit')!r}"
        )
    debts = _parse_debts(doc.get("debts"), source, problems)
    vendor = _parse_vendor_hashes(doc.get("vendor_hashes"), source, problems)

    if problems:
        raise BaselineError(problems)
    return Baseline(debts=debts, vendor_hashes=vendor)


def load_proposed_baseline(repo_root: Path, baseline_path: Path) -> Baseline:
    if not baseline_path.exists():
        raise BaselineError(
            [
                f"proposed baseline {baseline_path} not found in the candidate "
                "tree; restore it (or regenerate with --update-baseline)"
            ]
        )
    return parse_baseline(
        baseline_path.read_bytes(), f"proposed baseline {baseline_path}"
    )


def load_trusted_baseline(
    repo_root: Path, rev: str, baseline_name: str
) -> Baseline | None:
    """The baseline as of the comparison revision, or None when that
    revision predates this checker (bootstrap)."""
    check = _git_run(repo_root, "cat-file", "-e", f"{rev}:{baseline_name}")
    if check.returncode != 0:
        return None
    data = _git_out(repo_root, "cat-file", "blob", f"{rev}:{baseline_name}")
    return parse_baseline(data, f"trusted baseline at {rev}:{baseline_name}")


def derive_bootstrap_baseline(repo_root: Path, rev: str) -> Baseline:
    """Bootstrap: the comparison revision predates this checker, so the
    permitted debt is that tree's actual measured content and the vendor
    inventory starts empty (a bootstrap authorizes no hash exemptions)."""
    paths = tracked_rev_files(repo_root, rev)
    blobs = read_rev_blobs(repo_root, rev, paths)
    debts = {
        path: count_lines(data)
        for path, data in blobs.items()
        if count_lines(data) > HARD_LIMIT
    }
    return Baseline(debts=debts, vendor_hashes={})


def seed_bootstrap_debt_for_new_files(trusted: Baseline, candidate: Candidate) -> None:
    """Bootstrap only: files that do not exist at the comparison revision
    may enter the baseline at exactly their measured landing size (the
    landing diff is the review). The ceiling is pinned to candidate
    reality by the must-equal rule, so nothing beyond the visible diff is
    authorized; pre-existing files keep their comparison-tree ceilings and
    hash exemptions stay empty."""
    for path, size in candidate.sizes.items():
        if size > HARD_LIMIT and path not in trusted.debts:
            trusted.debts[path] = size


# ---------------------------------------------------------------------------
# Candidate tree measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    sizes: dict[str, int]
    hashes: dict[str, str]  # path -> sha256 of actual bytes


def measure_candidate(repo_root: Path) -> Candidate:
    sizes: dict[str, int] = {}
    hashes: dict[str, str] = {}
    for path in tracked_worktree_files(repo_root):
        # os.fspath round-trips surrogate-escaped names back to bytes.
        file_path = Path(os.path.join(os.fspath(repo_root), os.fspath(path)))
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            continue  # tracked but deleted in this working tree
        except IsADirectoryError:
            continue
        sizes[path] = count_lines(data)
        hashes[path] = hashlib.sha256(data).hexdigest()
    return Candidate(sizes=sizes, hashes=hashes)


def diff_against_rev(repo_root: Path, rev: str) -> tuple[dict[str, str], set[str]]:
    """Rename proof and changed-path set for the candidate working tree
    versus the comparison revision.

    Returns (renames, changed_paths) where `renames` maps each rename
    destination to its source path. Rename policy: `git diff -M` (50%
    similarity) proves `old -> new` is a rename; the *size* guard (new size
    <= the old path's trusted ceiling) proves it did not grow.
    """
    out = _git_out(repo_root, "diff", "--name-status", "--no-color", "-M", "-z", rev)
    tokens = [_decode(t) for t in out.split(b"\0")]
    renames: dict[str, str] = {}
    changed: set[str] = set()
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            break
        if status.startswith("R"):
            if i + 2 >= len(tokens):
                break  # truncated output; treat as no proof
            src, dst = tokens[i + 1], tokens[i + 2]
            renames[dst] = src
            changed.update((src, dst))
            i += 3
        else:
            if i + 1 >= len(tokens):
                break
            changed.add(tokens[i + 1])
            i += 2
    return renames, changed


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _vendor_refresh_allowed(
    trusted: Baseline,
    proposed: Baseline,
    changed_paths: set[str],
    baseline_name: str,
) -> bool:
    """The vendor-refresh exception: the candidate's whole diff touches
    only the baseline file and paths already pinned by the trusted vendor
    inventory, and the pinned path set is unchanged. Only then may a pin's
    hash be replaced (reviewed upstream bytes, same file)."""
    if not trusted.vendor_hashes:
        return False
    trusted_paths = set(trusted.vendor_hashes.values())
    proposed_paths = set(proposed.vendor_hashes.values())
    if trusted_paths != proposed_paths:
        return False
    return changed_paths <= trusted_paths | {baseline_name}


def _candidate_debt_failures(
    candidate: Candidate,
    trusted: Baseline,
    renames: dict[str, str],
    exemptions: dict[str, str],
) -> tuple[dict[str, int], list[str]]:
    """Which governed files may carry debt, and does any exceed its
    trusted permission? Returns (expected debts, failures)."""
    expected_debts: dict[str, int] = {}
    failures: list[str] = []
    for path in sorted(candidate.sizes):
        size = candidate.sizes[path]
        if candidate.hashes[path] in exemptions:
            continue  # content-hash exempt: not subject to line ceilings
        if size <= HARD_LIMIT:
            continue
        ceiling = trusted.debts.get(path)
        if ceiling is None:
            source = renames.get(path)
            if source is not None and source in trusted.debts:
                old_ceiling = trusted.debts[source]
                expected_debts[path] = size
                if size > old_ceiling:
                    failures.append(
                        f"{path}: {size} lines, but as a rename of "
                        f"{source} it may not exceed that path's trusted "
                        f"ceiling of {old_ceiling} lines; shrink it to "
                        f"{old_ceiling} lines or fewer"
                    )
                continue
            failures.append(
                f"{path}: {size} lines exceeds the {HARD_LIMIT}-line "
                "limit with no trusted baseline ceiling; new oversized "
                f"files must stay at or under {HARD_LIMIT} lines"
            )
            continue
        expected_debts[path] = size
        if size > ceiling:
            failures.append(
                f"{path}: grew to {size} lines, above its trusted "
                f"ceiling of {ceiling} lines; shrink it back to {ceiling} "
                "lines or fewer"
            )
    return expected_debts, failures


def _stale_entry_reason(path: str, candidate: Candidate) -> str:
    if path not in candidate.sizes:
        return "the file is deleted in the candidate tree"
    if not is_governed(path):
        return (
            "the path is not governed source (governed extensions: "
            + ", ".join(sorted(GOVERNED_EXTENSIONS))
            + ", outside vendored agent-tooling trees)"
        )
    if candidate.sizes[path] > HARD_LIMIT:
        return (
            "the file is over the limit with no trusted permission to "
            "carry debt (see the growth/new-file failure above)"
        )
    return (
        f"the file is {candidate.sizes[path]} lines, at or "
        f"under the {HARD_LIMIT}-line limit"
    )


def _mirror_failures(
    proposed: Baseline, expected_debts: dict[str, int], candidate: Candidate
) -> list[str]:
    """The proposed baseline must exactly mirror the candidate's
    remaining debt."""
    failures: list[str] = []
    for path in sorted(proposed.debts):
        lines = proposed.debts[path]
        if path not in expected_debts:
            reason = _stale_entry_reason(path, candidate)
            failures.append(
                f"{path}: stale baseline entry ({lines} lines) -- "
                f"{reason}; remove the entry"
            )
        elif lines != expected_debts[path]:
            failures.append(
                f"{path}: baseline ceiling {lines} lines must equal the "
                f"file's current size ({expected_debts[path]} lines); "
                f"set it to {expected_debts[path]}"
            )
    for path in sorted(expected_debts):
        if path not in proposed.debts:
            failures.append(
                f"{path}: {expected_debts[path]} lines over the "
                f"{HARD_LIMIT}-line limit but the proposed baseline has no "
                f"entry for it; record the debt as "
                f'{{"path": "{path}", "lines": {expected_debts[path]}}}'
            )
    return failures


def _vendor_pin_failures(
    proposed: Baseline, trusted: Baseline, refresh: bool, candidate: Candidate
) -> list[str]:
    """Vendor hash exemptions: subset of trusted (or a scoped refresh),
    each matching actual candidate bytes at its recorded path."""
    failures: list[str] = []
    for sha in sorted(proposed.vendor_hashes):
        recorded_path = proposed.vendor_hashes[sha]
        if sha not in trusted.vendor_hashes and not refresh:
            failures.append(
                f"{recorded_path}: new or replaced vendor content-hash "
                f"exemption ({sha[:12]}...) -- exemptions cannot be added "
                "in a routine PR; see scripts/README.md for the "
                "vendor-update policy"
            )
        if candidate.hashes.get(recorded_path) != sha:
            actual = candidate.hashes.get(recorded_path)
            detail = (
                f"current bytes hash to {actual[:12]}..."
                if actual
                else "the file is not present in the candidate tree"
            )
            failures.append(
                f"{recorded_path}: vendor exemption hash {sha[:12]}... "
                f"does not match actual bytes ({detail}); changed exempt "
                "bytes cease to be exempt -- update or remove the entry "
                "per the vendor-update policy"
            )
    return failures


def compare(
    candidate: Candidate,
    proposed: Baseline,
    trusted: Baseline,
    renames: dict[str, str],
    changed_paths: set[str],
    baseline_name: str,
) -> list[str]:
    """Return sorted, actionable failure lines; empty means the candidate
    passes."""
    # The vendor-refresh exception is decided first: when it applies, the
    # proposed pins (validated against actual bytes below) extend the
    # exemption set the candidate truth pass consults, so a reviewed
    # upstream refresh of an already-pinned file is not misread as new
    # debt. The scoping guarantees only bytes at already-pinned paths can
    # gain coverage this way.
    refresh = _vendor_refresh_allowed(trusted, proposed, changed_paths, baseline_name)
    exemptions = dict(trusted.vendor_hashes)
    if refresh:
        exemptions.update(proposed.vendor_hashes)

    expected_debts, failures = _candidate_debt_failures(
        candidate, trusted, renames, exemptions
    )
    failures.extend(_mirror_failures(proposed, expected_debts, candidate))
    failures.extend(_vendor_pin_failures(proposed, trusted, refresh, candidate))
    return sorted(failures)


# ---------------------------------------------------------------------------
# Baseline regeneration (never adds debt permission or exemptions)
# ---------------------------------------------------------------------------


def write_baseline(
    path: Path, debts: dict[str, int], vendor_hashes: dict[str, str]
) -> None:
    doc = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "hard_limit": HARD_LIMIT,
        "generated_by": f"{BASELINE_RELATIVE} --update-baseline",
        "debts": [{"path": p, "lines": debts[p]} for p in sorted(debts)],
        "vendor_hashes": [
            {"sha256": sha, "path": vendor_hashes[sha]} for sha in sorted(vendor_hashes)
        ],
    }
    path.write_text(
        json.dumps(doc, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _fail(problems: list[str]) -> None:
    print(f"::error::{len(problems)} file-size ratchet failure(s)", file=sys.stderr)
    for line in problems:
        print(line, file=sys.stderr)
    print(
        "\nReduce the file sizes listed above, tighten the proposed "
        "baseline to match (after a reduction):\n"
        f"  python {BASELINE_RELATIVE} --update-baseline\n"
        "then re-run the check:\n"
        f"  python {BASELINE_RELATIVE} --comparison-rev <revision>\n"
        "Policy: scripts/README.md (per-file size ratchet).",
        file=sys.stderr,
    )


def _cmd_check(repo_root: Path, baseline_path: Path, comparison_rev: str) -> int:
    rev = resolve_revision(repo_root, comparison_rev)
    candidate = measure_candidate(repo_root)
    proposed = load_proposed_baseline(repo_root, baseline_path)
    baseline_name = DEFAULT_BASELINE_NAME
    try:
        baseline_name = baseline_path.relative_to(repo_root).as_posix()
    except ValueError:
        pass  # baseline outside the repo: no refresh allowance for it

    trusted = load_trusted_baseline(repo_root, rev, baseline_name)
    bootstrap = trusted is None
    if bootstrap:
        trusted = derive_bootstrap_baseline(repo_root, rev)
        seed_bootstrap_debt_for_new_files(trusted, candidate)

    renames, changed_paths = diff_against_rev(repo_root, rev)
    failures = compare(
        candidate, proposed, trusted, renames, changed_paths, baseline_name
    )

    mode = (
        f"bootstrap: no baseline at {rev}; permitted debt derived from "
        "that tree's measured content (vendor inventory: empty)"
        if bootstrap
        else f"trusted baseline at {rev}"
    )
    if failures:
        _fail(failures)
        print(
            f"\n(context: {len(candidate.sizes)} governed tracked source "
            f"files measured in the candidate tree; comparison: {mode})",
            file=sys.stderr,
        )
        return EXIT_VIOLATIONS

    over = sum(1 for size in candidate.sizes.values() if size > HARD_LIMIT)
    print(
        f"OK: {len(candidate.sizes)} governed tracked source files; "
        f"{over} over the {HARD_LIMIT}-line limit, all matching the "
        f"{mode}."
    )
    return EXIT_OK


def _untracked_governed_files(repo_root: Path) -> list[str]:
    """Governed source paths visible on disk but not in the index -- the
    inventory (and so any regeneration) cannot see them."""
    out = _git_out(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(
        {
            decoded
            for raw in out.split(b"\0")
            if raw and is_governed(decoded := _decode(raw))
        }
    )


def _cmd_update(repo_root: Path, baseline_path: Path) -> int:
    candidate = measure_candidate(repo_root)

    untracked = _untracked_governed_files(repo_root)
    if untracked:
        print(
            f"::warning::{len(untracked)} untracked governed source file(s) "
            f"are NOT measured (the inventory is git ls-files): "
            f"{', '.join(untracked[:10])}"
            f"{' ...' if len(untracked) > 10 else ''}",
            file=sys.stderr,
        )
        print(
            "Stage or commit them before regenerating, or the baseline "
            "will silently omit their debt.",
            file=sys.stderr,
        )

    # Carry forward only vendor entries still matching actual candidate
    # bytes; regeneration never mints new exemptions.
    carried: dict[str, str] = {}
    if baseline_path.exists():
        try:
            existing = parse_baseline(
                baseline_path.read_bytes(), f"existing baseline {baseline_path}"
            )
        except BaselineError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            print(
                "Fix or hand-edit the baseline first; --update-baseline "
                "refuses to build on malformed data.",
                file=sys.stderr,
            )
            return EXIT_UNUSABLE
        for sha, recorded_path in existing.vendor_hashes.items():
            if candidate.hashes.get(recorded_path) == sha:
                carried[sha] = recorded_path

    debts = {
        path: size
        for path, size in candidate.sizes.items()
        if size > HARD_LIMIT and candidate.hashes[path] not in carried
    }
    write_baseline(baseline_path, debts, carried)
    print(
        f"Wrote {len(debts)} debt entr{'y' if len(debts) == 1 else 'ies'} "
        f"and {len(carried)} vendor hash exemption(s) to {baseline_path}. "
        "Regeneration never adds exemptions or authorizes growth -- "
        "validate with check mode:"
    )
    print(
        f"  python {BASELINE_RELATIVE} --comparison-rev "
        '"$(git merge-base HEAD origin/main)"'
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "exit 0 = pass, 1 = ratchet violations, 2 = the gate could "
            "not run (missing/malformed baseline, unresolvable revision)"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to check (default: this checkout's root)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=(
            "proposed baseline JSON path, inside --repo-root "
            f"(default: <repo-root>/{DEFAULT_BASELINE_NAME})"
        ),
    )
    parser.add_argument(
        "--comparison-rev",
        default=None,
        help=(
            "trusted comparison revision: a PR's merge-base with its "
            "target branch, or a push-to-main's before SHA (required in "
            "check mode)"
        ),
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help=(
            "regenerate the proposed baseline from the candidate tree "
            "(after a reduction) instead of checking; never adds debt "
            "permission or exemptions"
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    baseline_path = (
        args.baseline
        if args.baseline is not None
        else repo_root / DEFAULT_BASELINE_NAME
    )

    try:
        if args.update_baseline:
            return _cmd_update(repo_root, baseline_path)
        if not args.comparison_rev:
            parser.error(
                "--comparison-rev is required in check mode (e.g. the "
                "merge-base with the PR's target branch)"
            )
        return _cmd_check(repo_root, baseline_path, args.comparison_rev)
    except BaselineError as exc:
        for problem in exc.problems:
            print(f"::error::{problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    except GitError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_UNUSABLE


if __name__ == "__main__":
    raise SystemExit(main())
