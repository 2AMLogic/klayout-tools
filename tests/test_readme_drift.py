"""Drift guards for `README.md` and `examples/README.md` (issue #5419).

Two kinds of drift used to go unnoticed between releases:

1. `README.md` quoted a release version and a verb count that nothing checked,
   so both sat four minor versions stale ("v0.5.0 ... 44 verbs" while `main`
   was at v0.9.1 with 68 verbs).
2. A new directory could land under `examples/` without an entry in
   `examples/README.md`, so the index silently stopped listing everything.

`scripts/gen_site_cli_data.py` is the source of truth for both numbers. Its
output (`site/src/data/cli.generated.json`) is gitignored since issue #5005, so
these checks call the generator in-process rather than read the file: a plain
checkout has nothing on disk to read.

Note which number means what: `release.version` is the newest CHANGELOG
release, but `verb_count` counts the verbs on `main` (it is regenerated whenever
a verb lands, which is usually between releases). So the README may attribute
the version to the release and the count to `main` -- never the count to the
release.

The README carries NO literal release version (issue #5005). Patch releases are
automatic and the fleet publisher's bump commit changes exactly
`pyproject.toml` and `CHANGELOG.md`, so a version typed into the README is stale
the moment a patch release lands -- and no lag tolerance fixes that: any bound
still permits a stale claim after the next publication. The live PyPI badge and
project link carry the version instead, and a literal "vX.Y.Z is on PyPI" /
"PyPI X.Y.Z" claim fails here, which is also what keeps the original #5419
drift ("v0.5.0" while `main` was at v0.9.1) from coming back.
"""

from __future__ import annotations

import importlib.util
import re
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_site_cli_data.py"
README = REPO_ROOT / "README.md"
EXAMPLES = REPO_ROOT / "examples"
EXAMPLES_INDEX = EXAMPLES / "README.md"

# The two release-version claim shapes the README used to make, and nothing
# wider (it now makes neither; see the module docstring):
# the version token must be adjacent to "PyPI". Other version strings in
# README.md are not release claims and must not be matched -- the
# `klt-yield-native` wheel's own 0.1.0, the "0.9.1 and later" compatibility
# floor beside it, the `klayout-tools@v0.2.0` Action ref, and KLayout
# v0.30.10. `test_version_patterns_ignore_non_release_versions` pins that.
VERSION_PATTERNS = {
    '"vX.Y.Z is on PyPI"': re.compile(r"\bv(\d+\.\d+\.\d+) is on PyPI\b"),
    '"PyPI X.Y.Z"': re.compile(r"\bPyPI v?(\d+\.\d+\.\d+)\b"),
}
VERB_COUNT_PATTERN = re.compile(r"\b(\d+) verbs\b")


@cache
def _site_data() -> dict:
    spec = importlib.util.spec_from_file_location("gen_site_cli_data", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_data()


def _entry_marker(name: str) -> str:
    """The index's link form for a directory, e.g. ``[`sim/`](sim/)``.

    Matching the link form rather than the bare name matters: `sim` is a
    substring of `sim-batch` and `sim-remote`, and `yield` of
    `yield-sensitivity`, so a bare-name check would report those as indexed
    on the strength of their shorter sibling's entry.
    """
    return f"[`{name}/`]({name}/)"


def _unindexed(index_text: str, names: list[str]) -> list[str]:
    return [n for n in names if _entry_marker(n) not in index_text]


def _example_dirs() -> list[str]:
    return sorted(
        p.name
        for p in EXAMPLES.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
    )


def test_readme_states_no_literal_release_version():
    text = README.read_text(encoding="utf-8")
    for label, pattern in VERSION_PATTERNS.items():
        found = sorted(set(pattern.findall(text)))
        assert not found, (
            f"README.md makes a literal {label} release claim {found}: the "
            f"publisher's two-file bump commit cannot update it, so it goes "
            f"stale at the next automatic patch release. Drop the number and "
            f"let the PyPI badge and link carry the version (see this "
            f"module's docstring)"
        )


def test_readme_points_at_the_live_pypi_release():
    """Non-vacuity for the test above: the README must still say where the
    current version lives, so removing the number cannot leave it saying nothing."""
    text = README.read_text(encoding="utf-8")
    assert "img.shields.io/pypi/v/klayout-tools" in text, "the live PyPI badge is gone"
    assert "](https://pypi.org/project/klayout-tools/)" in text, (
        "README.md no longer links the PyPI project"
    )


def test_version_patterns_still_catch_a_literal_release_claim():
    """Positive control: the ban above is not vacuous."""
    for label, pattern, claim in (
        (
            '"vX.Y.Z is on PyPI"',
            VERSION_PATTERNS['"vX.Y.Z is on PyPI"'],
            "v0.9.2 is on PyPI",
        ),
        (
            '"PyPI X.Y.Z"',
            VERSION_PATTERNS['"PyPI X.Y.Z"'],
            "The latest release is PyPI 0.9.2;",
        ),
        ('"PyPI X.Y.Z"', VERSION_PATTERNS['"PyPI X.Y.Z"'], "released as PyPI v1.0.0"),
    ):
        assert pattern.findall(claim), (label, claim)


def test_readme_verb_count_matches_site_data():
    verb_count = str(_site_data()["verb_count"])
    text = README.read_text(encoding="utf-8")
    found = VERB_COUNT_PATTERN.findall(text)
    assert found, (
        "README.md no longer quotes a verb count; update VERB_COUNT_PATTERN "
        f"in {Path(__file__).name} if the wording moved on purpose"
    )
    assert set(found) == {verb_count}, (
        f"README.md quotes {sorted(set(found))} verbs, but "
        f"scripts/gen_site_cli_data.py reports verb_count {verb_count}"
    )


def test_version_patterns_ignore_non_release_versions():
    """The README's non-release version strings must not be matched."""
    not_release_claims = "\n".join(
        [
            "> As of 2026-11-06 the prebuilt `klt-yield-native` 0.1.0 wheel (Linux",
            "> x86_64, macOS arm64) is on PyPI, so `[yield]` resolves for "
            "klayout-tools",
            "> 0.9.1 and later. Linux aarch64, Windows and Intel macOS have "
            "no wheel and",
            "- uses: 2AMLogic/klayout-tools@v0.2.0",
            "  headless verification), tested on Apple Silicon with KLayout v0.30.10.",
            "[![PyPI](https://img.shields.io/pypi/v/klayout-tools)]"
            "(https://pypi.org/project/klayout-tools/)",
        ]
    )
    for label, pattern in VERSION_PATTERNS.items():
        assert not pattern.findall(not_release_claims), label


def test_every_examples_directory_is_indexed():
    dirs = _example_dirs()
    assert dirs, f"no example directories found under {EXAMPLES}"
    unindexed = _unindexed(EXAMPLES_INDEX.read_text(encoding="utf-8"), dirs)
    assert not unindexed, (
        "examples/README.md has no entry for: "
        + ", ".join(unindexed)
        + " -- add one bullet per directory in the existing "
        "[`name/`](name/) link form, describing what it actually contains"
    )


def test_a_shorter_siblings_entry_does_not_count_as_an_entry():
    index = "- [`sim/`](sim/) - corner-matrix requests\n- [`yield/`](yield/)\n"
    assert _unindexed(index, ["sim", "yield"]) == []
    assert _unindexed(index, ["sim-batch", "sim-remote", "yield-sensitivity"]) == [
        "sim-batch",
        "sim-remote",
        "yield-sensitivity",
    ]
