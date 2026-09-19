# Batch-fleet worked example: 31-stage sky130 ring oscillator

The `batch` backend's counterpart to
[`examples/sim-remote/`](../sim-remote/README.md)
([#2080](https://github.com/2AMLogic/klayout-tools/issues/2080)): the
**same** 5-process-corner matrix and the **same** testbench, submitted to
[2AMLogic/2am](https://github.com/2AMLogic/2am)'s EDA batch fleet as an S3
job contract instead of provisioning an EC2 instance from this host.

`matrix-batch.request.json` differs from
[`matrix-remote.request.json`](../sim-remote/matrix-remote.request.json) in
exactly two places: `backend` is `"batch"`, and the `remote` provisioning
block is replaced by a `batch` submit block. Everything else — netlist,
models, corners, analysis, measurements, options — is byte-identical, which
is the point: the fleet runs the same `klt sim ... --backend local-parallel`
invocation the other two backends run, so the report shape does not change
with the backend.

## Artifacts

| File | Description |
| --- | --- |
| [`ring_tb.spice`](ring_tb.spice) | The testbench, copied verbatim from `examples/sim-remote/` so this example stands alone: 31-stage sky130 ring oscillator, a 20fF load cap per stage, a `PULSE` kick source, `.options reltol=1e-4` |
| [`matrix-batch.request.json`](matrix-batch.request.json) | 5-process-corner matrix (`tt`/`ss`/`ff`/`sf`/`fs` × 1.8V × 27°C), `backend: "batch"` |
| `matrix-batch.report.json` | **Deliberately absent** — see "Live-run slot" below |

The `local` reference report for this identical matrix is already committed
next door as
[`examples/sim-remote/matrix-local.report.json`](../sim-remote/matrix-local.report.json);
regenerate it (no AWS credentials needed, but a resolvable `sky130A` PDK and
`ngspice` on `PATH`) with:

```sh
uv run klt sim examples/sim-remote/matrix-local.request.json --format json
```

## Running the batch request

```sh
uv run klt sim examples/sim-batch/matrix-batch.request.json --format json
```

`matrix-batch.request.json` ships documented placeholders for both
deployment-specific fields — never a working path from someone's machine,
and never a real job-bucket name:

```json
"batch": {
  "bucket": "<your-batch-jobs-bucket>",
  "region": "us-east-1",
  "profile": "batch-runner-submit",
  "provision_script_path": "<path-to-your-2am-checkout>/infra/aws/batch-fleet-provision.sh",
  "poll_interval_s": 30,
  "poll_timeout_s": 5400
}
```

Point `provision_script_path` at your own `2am` checkout (or set
`$KLT_BATCH_PROVISION_SCRIPT` and delete the field), and replace
`<your-batch-jobs-bucket>` with your own job bucket (or set
`$KLT_BATCH_JOB_BUCKET`). `bucket`/`region`/`profile` can all be deleted too — with the script path resolved, `klt`
reads them from the `batch-fleet.env` sitting beside it, which is 2am's own
source of truth for those values. Nothing here is a credential: `profile` is
an `aws` CLI profile *name*, and the key it resolves to lives in your own
AWS config.

See [`docs/cli/sim.md`'s "Batch backend"](../../docs/cli/sim.md#batch-backend)
for the full field table and the four-step seam this implements.

## Live-run slot: deliberately empty

**No `matrix-batch.report.json` is committed, on purpose.** A committed
report here would have to be either a real measurement or a fabrication, and
the batch fleet has never run a job: as of this example's authoring, the
2am-side prerequisites are all outstanding —

- `build-batch-image.sh bake --apply` has never run (no batch AMI exists),
- `batch-fleet-provision.sh provision --apply` has never run (the job
  bucket does not exist),
- the `batch-runner-submit` access key has never been minted,
- 2am#533's scheduled `reconcile` timer is not installed on the captain.

None of those block the backend or its tests — every AWS/launch call in
`src/klayout_tools/sim_batch.py` goes through an injectable command runner,
and `tests/test_sim_batch.py` exercises submit/poll/collect end to end
against a recorded stub with no AWS and no network. They block only a *live*
measurement.

When those steps are done, run the command above and commit the resulting
report here, alongside a measured comparison table in the shape
`examples/sim-remote/README.md` already uses (wall-clock per corner, total,
cost). Until then this section is the honest statement of what has and has
not been demonstrated.
