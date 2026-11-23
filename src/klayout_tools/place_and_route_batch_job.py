"""Build the worker command and inputs for one place-and-route batch unit."""

from __future__ import annotations

import json
import shlex
from typing import Any

from . import _batch_common, sim_batch
from .place_and_route import PlaceAndRouteError

#: The tree input and the per-unit request under ``inputs/``.
BATCH_TREE_FILENAME = "tree.tar.gz"
BATCH_REQUEST_FILENAME = "request.json"

#: The stage-output directory the runner writes under the extracted tree:
#: recorded in ``_batch_common.BATCH_TREE_ROOT_FILENAME`` so collected
#: reports can be re-rooted, and copied out to
#: ``$EDA_OUTPUT_DIR/artifacts/``.
STAGE_DIRNAME = ".klt/place-and-route"

#: ``job.json``'s ``tool`` label.
BATCH_JOB_TOOL = "openroad"

BATCH_THREADS_EXCEED_VCPUS_CODE = "batch_threads_exceed_vcpus"


def _thread_preflight(threads: int | str) -> str:
    """Resolve max or refuse an undersized worker before any routing starts."""
    capacity = "klt_vcpus=$(nproc) || exit 1; "
    if threads == "max":
        return capacity + "klt_threads=$klt_vcpus; "
    envelope = json.dumps(
        {
            "schema_version": 1,
            "error": {
                "command": "place-and-route",
                "code": BATCH_THREADS_EXCEED_VCPUS_CODE,
                "message": "requested %s OpenROAD threads but the worker has %s vCPUs",
            },
        }
    )
    fmt = shlex.quote(envelope + "\n")
    return (
        capacity
        + f"klt_threads={shlex.quote(str(threads))}; "
        + 'if ! [ "$klt_threads" -le "$klt_vcpus" ]; then '
        + f'printf {fmt} "$klt_threads" "$klt_vcpus" '
        + f'> "$EDA_OUTPUT_DIR/{sim_batch.BATCH_REPORT_FILENAME}"; exit 1; fi; '
    )


def build_job_spec(
    *,
    request_name: str,
    unit: dict[str, Any],
    tarball_path: str,
    pdk_variant: str | None,
    timeout_s: float,
    version_check: str = "enforce",
) -> sim_batch.BatchJobSpec:
    """One unit's job as a :class:`~klayout_tools.sim_batch.BatchJobSpec`.

    ``cmd`` is the runner-version preflight, then: extract the tree into a
    fresh directory, record that directory's path, install the unit request
    over the original, and run ``klt place-and-route`` there with ``--backend
    local --format json``. The report goes to ``report.json`` and stderr is
    teed to ``stderr.log`` (where klt writes an error envelope), the channels
    ``klt sim``'s batch job uses; the stage output is copied to
    ``$EDA_OUTPUT_DIR/artifacts/`` whether or not the run succeeded.
    """
    name = shlex.quote(request_name)
    argv = ["klt place-and-route", name]
    if pdk_variant:
        argv += ["--pdk", shlex.quote(pdk_variant)]
    argv += ["--backend local --format json"]
    preflight = sim_batch.par_backend_preflight_lines()
    if "threads" in unit:
        preflight += _thread_preflight(unit["threads"])
        argv.append('--threads "$klt_threads"')
    command = _batch_common.build_job_command(
        tree_member=BATCH_TREE_FILENAME,
        run=" ".join(argv),
        err_cls=PlaceAndRouteError,
        stdout_target=sim_batch.BATCH_REPORT_FILENAME,
        version_check=version_check,
        preflight_extra=preflight,
        extra_setup=(f'cp "$EDA_INPUT_DIR/{BATCH_REQUEST_FILENAME}" "$w/"{name}',),
        tree_root_subdir=STAGE_DIRNAME,
        extra_tail=(
            f"if [ -d {STAGE_DIRNAME} ]; then "
            f'mkdir -p "$EDA_OUTPUT_DIR/{sim_batch.BATCH_ARTIFACTS_DIRNAME}" && '
            f"cp -a {STAGE_DIRNAME}/. "
            f'"$EDA_OUTPUT_DIR/{sim_batch.BATCH_ARTIFACTS_DIRNAME}/"; fi'
        ),
    )
    return _batch_common.build_job_spec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=pdk_variant,
        tree_member=BATCH_TREE_FILENAME,
        tree_label="request tree",
        tarball_path=tarball_path,
        timeout_s=timeout_s,
        # Deliberately NOT _batch_common.job_timeout_seconds': unlike every
        # sibling backend this command wraps `klt place-and-route` in no inner
        # `timeout`, so there is nothing for that headroom to fire ahead of,
        # and `request.batch.job_timeout_s` is documented as the bound on one
        # job (docs/cli/place-and-route.md). Adding the overhead would push
        # the real cap past the number the operator set.
        timeout_seconds=int(timeout_s),
        extra_inputs=(
            sim_batch.BatchJobInput(
                name=BATCH_REQUEST_FILENAME,
                label="unit request",
                content=json.dumps(unit),
            ),
        ),
    )
