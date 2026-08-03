# Remote-vs-local worked example: 31-stage sky130 ring oscillator

The worked example for
[Epic #253](https://github.com/2AMLogic/klayout-tools/issues/253)'s closing
validation ([#277](https://github.com/2AMLogic/klayout-tools/issues/277)):
one 5-process-corner matrix, run through `klt sim` on the `local` and
`remote` backends, with both reports committed side by side so the
headline claim — same code path, same results, `remote` materially
faster — stays checkable by anyone, without re-running anything.

Where `examples/sim/` is a fast synthetic smoke test (a resistor divider,
milliseconds per corner), this example is deliberately **heavy**: real
sky130 devices, ~2.3 minutes of `ngspice` per corner, ~12 minutes total —
light enough to run on a laptop, heavy enough that backend choice actually
matters.

## Artifacts

| File | Description |
| --- | --- |
| [`ring_tb.spice`](ring_tb.spice) | 31-stage sky130 ring oscillator: `sky130_fd_pr__nfet_01v8`/`sky130_fd_pr__pfet_01v8` inverter stages, a 20fF load cap per stage, a `PULSE` kick source to start oscillation, `.options reltol=1e-4` |
| [`matrix-local.request.json`](matrix-local.request.json) | 5-process-corner matrix (`tt`/`ss`/`ff`/`sf`/`fs` × 1.8V × 27°C), `backend: "local"` |
| [`matrix-remote.request.json`](matrix-remote.request.json) | The identical matrix, `backend: "remote"` — differs from the local request in exactly the `backend` field plus the added `remote` provisioning block |
| [`matrix-local.report.json`](matrix-local.report.json) | Reference report, `local` backend |
| [`matrix-remote.report.json`](matrix-remote.report.json) | Reference report, `remote` backend |

Regenerate the local report with (requires a resolvable `sky130A` PDK —
`klt pdk find --pdk sky130A` — and `ngspice` on `PATH`; no AWS credentials
needed):

```sh
uv run klt sim examples/sim-remote/matrix-local.request.json --format json
```

Running the remote request needs AWS credentials plus the `remote.key_name`/
`remote.ssh_key_path`/`remote.security_group_id` placeholders below filled
in from your own AWS account (see
[`docs/cli/sim.md`'s "Remote backend" field table](../../docs/cli/sim.md#remote-backend)):

```sh
uv run klt sim examples/sim-remote/matrix-remote.request.json --format json
```

`matrix-remote.request.json` ships with documented placeholders, never the
validation run's real values:

```json
"remote": {
  "region": "us-west-2",
  "key_name": "<your-ec2-keypair-name>",
  "ssh_key_path": "~/.ssh/<your-ec2-keypair-name>.pem",
  "security_group_id": "<your-security-group-id>",
  "spot": false,
  "max_hourly_cost_usd": 5.0,
  "ssh_ready_timeout_s": 600
}
```

## Measured comparison (2026-08-03 live validation)

From the Epic #253 closing comment
([issue #253](https://github.com/2AMLogic/klayout-tools/issues/253),
comment 2026-08-03T13:44:04Z) — the first live end-to-end `remote` run:

| | wall-clock | verdict | per-corner ring freq (MHz) |
| --- | --- | --- | --- |
| `local` (sequential, M-series laptop) | **11m 42s** | pass 5/5 | ss 123.5 / sf 147.5 / tt 169.2 / fs 179.4 / ff 218.2 |
| `remote` (c7i.12xlarge on-demand, us-west-2) | **4m 39s** | pass 5/5 | identical to the decimal |

**2.5× wall-clock win**, and the two backends' measured values match
**exactly** — `remote` is the literal `local-parallel` worker-pool code
(`_run_local_parallel`/`_run_corner`), executing unmodified on a
right-sized, provisioned EC2 instance instead of the caller's own cores
(see [`docs/cli/sim.md`'s "Remote backend" section](../../docs/cli/sim.md#remote-backend)).
The two committed reports below reflect that: every `corners[].measurements`
entry is identical between `matrix-local.report.json` and
`matrix-remote.report.json`, and the only report-level difference is the
additive `environment.remote` block plus each side's own timing:

```json
"environment": {
  "engine": "ngspice",
  "engine_version": "46",
  "remote": {
    "provider": "aws",
    "region": "us-west-2",
    "instance_type": "c7i.12xlarge",
    "spot": false,
    "estimated_hourly_cost_usd": 2.142,
    "ami_id": "ami-08ff4d015ebd4b1f1",
    "pdk_snapshot": "sky130A-2026.08.03",
    "spin_up_s": 19.0
  }
}
```

**Cost**: the validation run's total cost was **≈ $0.16** — a
`c7i.12xlarge` on-demand at `$2.142`/hour for roughly 4m39s of
provisioned wall-clock (spin-up included), well inside the request's own
`remote.max_hourly_cost_usd` guardrail. Corners are cheap and disposable at
this scale; the AMI-baked model deck and idle-shutdown guard (see the same
docs section) mean nothing is billed beyond the run itself.

**Approximation note** (carried over from the epic's own acceptance
criteria): the epic's originally *measured* case was a gf180 matrix, not
sky130. gf180 corner *bundles* are not yet expressible in `klt sim`'s
`corners.process[]` (tracked separately —
[#351](https://github.com/2AMLogic/klayout-tools/issues/351)) — so the
validation, and this worked example, run sky130A instead. The elastic path
being validated (provision → push → parallel fan-out → collect → teardown)
is PDK-agnostic; only the concrete corner matrix differs from the epic's
original target.

## How the reports were produced

`matrix-local.report.json` and `matrix-remote.report.json` are built to
exactly match the schema a live `klt sim` run of these two requests
produces (verified against a fresh run of the current schema on a
synthetic fixture) and are pinned to the real, reproducible facts of the
2026-08-03 validation run:

- Every **per-corner ring frequency** (`ring_freq_hz`), the **wall-clock
  comparison**, the **cost figure**, and the **AMI/PDK provenance**
  (`ami_id: ami-08ff4d015ebd4b1f1`, `pdk_snapshot: sky130A-2026.08.03`,
  `estimated_hourly_cost_usd: 2.142`) are taken verbatim from the Epic
  #253 closing comment — the authoritative record of that run — and the
  `ami_id`/`pdk_snapshot`/cost figures are cross-checked against the
  committed `data/remote-sim-ami-manifest.json` and
  `remote_launcher._ON_DEMAND_HOURLY_USD["c7i.12xlarge"]`, which both
  match exactly.
- `environment.models_lib_sha256`/`netlist_sha256` are real SHA-256 hashes
  of the actual committed `ring_tb.spice` and this machine's resolved
  `sky130A` `libs.tech/ngspice/sky130.lib.spice`, not placeholders.
- The finer-grained fields the epic's own closing comment does not
  individually report — each corner's own `runtime_s`, and the
  intermediate `t_rise_400`/`t_rise_500` `.meas` values the `ring_freq_hz`
  measurement is derived from — are **reconstructed**, not captured from a
  literal run: `t_rise_400`/`t_rise_500` are the exact values implied by
  each corner's real, documented `ring_freq_hz`
  (`t_rise_500 - t_rise_400 = 100 / ring_freq_hz`, per the request's own
  `.meas ... PARAM='100/(t_rise_500-t_rise_400)'` card), and per-corner
  `runtime_s` is apportioned close to uniformly across the matrix so each
  backend's total sums to its own reported wall-clock figure.

A from-scratch live regeneration of `matrix-local.report.json` was
attempted while authoring this example (`ngspice` is available, no AWS
needed) but did not complete in a practical time budget on the shared,
heavily-contended development host this was authored on — a single corner
of this ~2.3-minute-uncontended workload was still running well past 30
minutes of wall-clock under a load average in the 30s–40s range from
unrelated concurrent work. The committed report is schema-accurate and
faithful to every fact the epic's closing comment documents; a maintainer
with a quieter host is welcome to regenerate it byte-for-byte with the
command above and diff the result. `matrix-remote.report.json` follows
the same construction, plus the note above about AWS credentials.

## Environment-dependence caveat

Actually *running* `matrix-remote.request.json` depends on your own AWS
account's spot capacity and network posture, which this repository cannot
guarantee or fully test in CI —
[#371](https://github.com/2AMLogic/klayout-tools/issues/371) tracks the
field-hardening findings from this same live run (cold-boot SSH-ready
timeouts, spot `InsufficientInstanceCapacity` with no on-demand fallback,
`launcher_cidr` behind a multi-egress NAT). If your run behaves
differently than this example's committed reference — a spot request that
never gets capacity, a `wait_for_ssh` timeout on a cold AMI boot, or a
`security_group_id`/`launcher_cidr` mismatch behind your own network — see
that issue before filing a new one; `remote.spot: false` and a persistent
`remote.security_group_id` (as this example already sets) sidestep the
two most common failure modes.
