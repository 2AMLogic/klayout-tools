#!/usr/bin/env python3
"""Pre-publish guard: every `klt-*-native` extra pin must be on PyPI (issue #2900).

`pyproject.toml`'s optional extras pin the prebuilt Rust wheels
(`klt-yield-native>=0.1.0,<0.2`). A klayout-tools release that carries such an
extra before the pinned package is published makes `pip install
'klayout-tools[yield]'` unresolvable -- exactly what v0.6.0 and v0.7.0 shipped.
`publish.yml` runs this before `uv publish` and fails the release in that case.

For each requirement in `[project.optional-dependencies]` whose name matches
`klt-*-native`, the lower bound (`>=X` / `==X` / `~=X`) must resolve at
`https://pypi.org/pypi/<pkg>/<ver>/json`.

Exit codes: 0 all pins resolve (or none exist); 1 a pin is not published;
2 PyPI could not be queried (5xx / network) -- fail closed with a distinct
message, since "unreachable" is not "unpublished".

Usage::

    uv run --python 3.12 python scripts/check_native_extras.py \
        [--pyproject pyproject.toml]
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

NATIVE_NAME_RE = re.compile(r"^klt-[a-z0-9]+(?:-[a-z0-9]+)*-native$")
REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
LOWER_BOUND_RE = re.compile(r"(?:>=|==|~=)\s*([0-9][A-Za-z0-9.+!_-]*)")

PUBLISHED, MISSING, UNREACHABLE = "published", "missing", "unreachable"

DOCS = "docs/releasing-native.md"

Fetch = Callable[[str], int]


def http_status(url: str) -> int:
    """HTTP status for `url`; 0 when the network itself failed."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def native_pins(pyproject: dict) -> list[tuple[str, str, str]]:
    """`(extra, package, lower-bound version)` for each klt-*-native pin."""
    pins = []
    extras = pyproject.get("project", {}).get("optional-dependencies", {})
    for extra, reqs in extras.items():
        for req in reqs:
            m = REQ_RE.match(req)
            if not m or not NATIVE_NAME_RE.match(m.group(1).lower()):
                continue
            lb = LOWER_BOUND_RE.search(m.group(2))
            pins.append((extra, m.group(1).lower(), lb.group(1) if lb else ""))
    return pins


def check(pyproject: dict, fetch: Fetch = http_status) -> list[tuple[str, str]]:
    """Return `(state, message)` per problem; empty when the guard passes."""
    problems = []
    for extra, pkg, ver in native_pins(pyproject):
        if not ver:
            problems.append(
                (MISSING, f"extra [{extra}] pins {pkg} with no lower-bound version")
            )
            continue
        url = f"https://pypi.org/pypi/{pkg}/{ver}/json"
        status = fetch(url)
        if status == 200:
            continue
        if status == 404:
            problems.append(
                (
                    MISSING,
                    f"extra [{extra}] pins {pkg}>={ver}, which is not published on "
                    f"PyPI ({url} -> 404). Publish it first; see {DOCS}.",
                )
            )
        else:
            problems.append(
                (
                    UNREACHABLE,
                    f"could not verify {pkg}=={ver} for extra [{extra}] "
                    f"({url} -> {status or 'network error'}); failing closed. "
                    "Re-run the release once PyPI is reachable.",
                )
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pyproject", default="pyproject.toml")
    args = ap.parse_args(argv)
    # Deferred: tomllib is stdlib only from 3.11 (the project floor is 3.10),
    # and `check()` takes a plain dict so tests never need it.
    import tomllib

    data = tomllib.loads(Path(args.pyproject).read_text())
    problems = check(data)
    for _, msg in problems:
        print(f"error: {msg}", file=sys.stderr)
    if any(s == MISSING for s, _ in problems):
        return 1
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
