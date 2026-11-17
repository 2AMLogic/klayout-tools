"""Expectation flags for ``klt pdk find``/``check`` (issues #3810, #5624).

Arg registration, the ``--expect-bundle-sha256`` report assembly, and the
shared exit handling: every expectation verdict that is ``mismatch`` or
``unknown`` (fail closed) exits ``6`` -- the payload is still emitted, and
``check``'s lower codes (broken symlinks, ``4``) win. The exit-code constant
stays in :mod:`.pdk_cmd` beside its sibling ``klt pdk`` codes; callers pass
it in. See ``docs/cli/pdk.md``.
"""

import argparse
import os
import sys

from ..pdk_compat import check_expected_bundle


def add_expect_version_arg(parser: argparse.ArgumentParser) -> None:
    """Register ``--expect-version`` on ``klt pdk find``/``check`` (issue #3810)."""
    parser.add_argument(
        "--expect-version",
        dest="expect_version",
        default=None,
        metavar="STAMP_OR_SHA",
        help=(
            "assert the installed PDK identity: an open_pdks commit SHA "
            "(>= 7 hex) or SOURCES stamp, or a fetched-version string. Exit 6 "
            "(payload still emitted) on mismatch or unknown identity"
        ),
    )


def add_expect_bundle_arg(parser: argparse.ArgumentParser) -> None:
    """Register ``--expect-bundle-sha256`` on ``klt pdk check`` (issue #5624)."""
    parser.add_argument(
        "--expect-bundle-sha256",
        dest="expect_bundle_sha256",
        default=None,
        metavar="SHA256",
        help=(
            "assert the install was provisioned from the bundle whose "
            "recorded sha256 this is (full 64-hex digest, compared exactly "
            "with the nearest .pdk-store.json; the stamp is trusted, the "
            "tree is not rehashed). Exit 6 (payload still emitted) on "
            "mismatch or unknown (no recorded sha256)"
        ),
    )


def bundle_expectation(report: dict, expected: str | None) -> None:
    """Add ``bundle_expectation`` to a resolved report when ``klt pdk check``
    was given ``--expect-bundle-sha256`` (``check`` only; ``find`` never
    registers the flag). ``expected`` is compared with the ``sha256`` the
    install's nearest ``.pdk-store.json`` records (issue #5624). The variant
    directory is resolved the way :func:`klayout_tools.pdk.find_pdk`'s probe
    chose it: ``root``/``variant`` when that directory exists, else ``root``
    itself -- a flat single-PDK install's variant directory is the root, not
    the join (issue #522; see ``pdk._probe_root``'s flat fallback), so both
    expectation legs read the same directory and stamp placements.
    """
    if expected is None:
        return
    variant_dir = os.path.join(report["root"], report["variant"])
    if not os.path.isdir(variant_dir):
        variant_dir = report["root"]
    report["bundle_expectation"] = check_expected_bundle(variant_dir, expected)


#: The expectation objects ``find``/``check`` may carry, in report order:
#: report key and the noun naming the expectation in its one-line stderr
#: failure (issue #5624 adds the bundle leg, ``check`` only).
_EXPECTATIONS: tuple[tuple[str, str], ...] = (
    ("version_expectation", "version"),
    ("bundle_expectation", "bundle"),
)


def expectations_exit(report: dict, mismatch_exit: int) -> int:
    """Exit code (``mismatch_exit`` or ``0``) for the expectation flags; on
    each failure writes one stderr line naming both values. All failing
    expectations report (one line each) even though the code is shared."""
    code = 0
    for key, noun in _EXPECTATIONS:
        result = report.get(key)
        if not result or result["status"] == "match":
            continue
        installed = result["installed"]
        print(
            f"klt: PDK {noun} expectation {result['status']}: expected "
            f"{result['expected']!r}, installed "
            f"{installed if installed is not None else 'unknown'!r}",
            file=sys.stderr,
        )
        code = mismatch_exit
    return code
