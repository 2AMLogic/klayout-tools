"""What a functional-verification run simulated: the resolved-sources echo.

Issue #5771. A ``klt.functional_verification.request/1`` report used to name
the engine, the per-test results and the environment but not *which files were
simulated*, so an RTL run and a gate-level run of the same testbench module
produced reports that differed only in ``real_time_s`` jitter. The report now
carries an additive ``environment.sources`` array: one ``{path, scope,
sha256}`` entry per resolved source, in build order.

``path``/``scope`` is the shared ``env_provenance.repo_relative_path``
shape (never an absolute host path -- a source outside the repo reads
``{"path": null, "scope": "external"}``); ``sha256`` is the digest of the
file's bytes, so a source edited after the run is detectable and two paths
sharing a basename are still told apart.

The echo is taken from the list ``options.from_synthesize`` resolution
returns -- *before* the SDF/initial-state/scoped-define build steps add
generated files -- so it names the caller's own inputs, not tool scratch. When
``options.from_synthesize`` is used, its verified netlist is the first entry.

Lives apart from ``functional_verification.py`` (size-ratchet baseline): both
report builders (cocotb and Verilog-top) take the echo through the one
:func:`with_run_inputs` post-step.

Both builders also attach the shared ``provenance`` block through this
post-step (issue #5550).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from typing import Any, NamedTuple

from ._provenance import (
    INPUT_ROLE_SOURCE,
    _combined_content_hash,
    build_provenance,
    content_hash,
    sha256_file,
    sha256_file_required,
)
from .env_provenance import find_repo_root, repo_relative_path
from .functional_verification_base import (
    FunctionalVerificationError,
)
from .functional_verification_from_synthesize import (
    resolve_sources_with_synthesize,
    with_from_synthesize,
)
from .functional_verification_pdk import derive_pdk_defines, merge_pdk_input


class RunInputs(NamedTuple):
    """The resolved inputs a report echoes (additive fields only)."""

    from_synthesize: dict[str, Any] | None
    sources: list[dict[str, Any]]
    #: Defines the ``pdk`` input derives (``FUNCTIONAL``, no SDF); the run
    #: merges them *under* the caller's own ``options.defines`` (issue #5782).
    pdk_defines: dict[str, str | None]
    #: Original source paths for normalizing simulator failure messages.
    source_paths: list[str]
    #: Captured before generated wrappers or SDF rewrites enter the build.
    provenance: dict[str, Any]

    def pin_sdf(self, sdf: dict[str, str] | None) -> None:
        if sdf is not None:
            try:
                self.provenance["sdf_sha256"] = sha256_file_required(sdf["file"])
            except OSError as exc:
                raise FunctionalVerificationError(
                    f"Cannot hash original SDF {sdf['file']!r}: {exc}"
                ) from exc

    def normalize_failure_messages(
        self, tests: list[dict[str, Any]], request_dir: str
    ) -> None:
        """Render known RTL filenames in parsed cocotb diagnostics for the report."""
        failures = [test for test in tests if test.get("error_message")]
        if not failures:
            return
        root = find_repo_root(request_dir) or request_dir
        labels: dict[str, str] = {}
        for i, source in enumerate(self.source_paths):
            relative = repo_relative_path(source, repo_root=root)["path"]
            label = relative or f"environment.sources[{i}]"
            labels.setdefault(source, label)
            labels.setdefault(os.path.realpath(source), label)

        # Match complete filenames at diagnostic delimiters. A root scrubber
        # would also match prefixes of other names such as "cells.v~". A
        # parenthesized line number -- "gcd.v(17):" -- ends a filename, while
        # a reference opening right after a quote is whole: it runs to the
        # closing quote or a location suffix, so a space inside quotes is
        # filename text ("gcd.v (copy).v"), not a delimiter.
        paths = "|".join(re.escape(p) for p in sorted(labels, key=len, reverse=True))
        location = re.compile(
            r"""(?:(?<=["'`])"""
            rf"(?:{paths})"
            r"""(?=["'`]|$|:|\((?=\d))"""
            r"""|(?<!["'`])(?<![^\s'"`(<\[{:;,=])"""
            rf"(?:{paths})"
            r"""(?=$|[\s'"`)>\]}:;,]|\.(?=\s|$)|\((?=\d)))"""
        )
        for test in failures:
            test["error_message"] = location.sub(
                lambda match: labels[match[0]], test["error_message"]
            )


def sources_echo(
    sources: list[str], envelope: Callable[[str | None], dict[str, Any]]
) -> list[dict[str, Any]]:
    """``[{path, scope, sha256}, ...]`` for ``sources``, in order.

    ``envelope`` is the ``path -> {path, scope}`` normalizer the verb already
    uses for its other path fields.
    """
    return [{**envelope(src), "sha256": sha256_file(src)} for src in sources]


def resolve_run_inputs(
    request_doc: dict[str, Any],
    request_dir: str,
    hdl_toplevel: str,
    envelope: Callable[[str | None], dict[str, Any]],
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    verify_synthesis_pin: bool = True,
) -> tuple[list[str], RunInputs]:
    """:func:`resolve_sources_with_synthesize` plus the ``sources`` echo.

    ``pdk_variant``/``pdk_root`` (the CLI's ``--pdk``/``--pdk-root``, issue
    #5782) merge over the request's ``pdk`` block here; with no pdk input at
    all the result is byte-identical to before the issue."""
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise FunctionalVerificationError(
            "request.hdl_toplevel must be a non-empty string"
        )
    pdk_input = merge_pdk_input(request_doc, request_dir, pdk_variant, pdk_root)
    sources, synth_block = resolve_sources_with_synthesize(
        request_doc,
        request_dir,
        hdl_toplevel,
        envelope,
        pdk_input=pdk_input,
        verify_synthesis_pin=verify_synthesis_pin,
    )
    defines = derive_pdk_defines(pdk_input, request_doc.get("options"))
    canonical = json.dumps(request_doc, sort_keys=True, separators=(",", ":"))
    provenance: dict[str, Any] = {
        "request_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "input": None,
    }
    # Only RTL shares a source-role hash with synthesize/equiv. A gate-level
    # run records its netlist pin separately in from_synthesize.
    if synth_block is None:
        digest = (
            content_hash(sources[0])
            if len(sources) == 1
            else _combined_content_hash(sources)
        )
        provenance["input"] = {"role": INPUT_ROLE_SOURCE, "content_hash": digest}
    return sources, RunInputs(
        synth_block, sources_echo(sources, envelope), defines, list(sources), provenance
    )


def with_run_inputs(report: dict[str, Any], inputs: RunInputs) -> dict[str, Any]:
    """Attach the resolved inputs and their provenance to the report in place."""
    with_from_synthesize(report, inputs.from_synthesize)
    report["environment"]["sources"] = inputs.sources
    report["provenance"] = {**build_provenance(), **inputs.provenance}
    return report
