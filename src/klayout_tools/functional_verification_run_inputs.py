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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from ._provenance import sha256_file
from .functional_verification_from_synthesize import (
    resolve_sources_with_synthesize,
    with_from_synthesize,
)


class RunInputs(NamedTuple):
    """The resolved inputs a report echoes (additive fields only)."""

    from_synthesize: dict[str, Any] | None
    sources: list[dict[str, Any]]


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
) -> tuple[list[str], RunInputs]:
    """:func:`resolve_sources_with_synthesize` plus the ``sources`` echo."""
    sources, synth_block = resolve_sources_with_synthesize(
        request_doc, request_dir, hdl_toplevel, envelope
    )
    return sources, RunInputs(synth_block, sources_echo(sources, envelope))


def with_run_inputs(report: dict[str, Any], inputs: RunInputs) -> dict[str, Any]:
    """Attach ``from_synthesize`` and ``environment.sources`` (in place)."""
    with_from_synthesize(report, inputs.from_synthesize)
    report["environment"]["sources"] = inputs.sources
    return report
