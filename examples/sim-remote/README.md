# `klt sim` worked example: `remote` vs. `local` backend

A worked example for [Epic #253](https://github.com/2AMLogic/klayout-tools/issues/253)'s
closing validation ([#277](https://github.com/2AMLogic/klayout-tools/issues/277)):
a heavy-enough-to-matter corner matrix, run through both the `local` and
`remote` execution backends described in
[`docs/cli/sim.md`'s "Execution backends"](../../docs/cli/sim.md#execution-backends)
section. `examples/sim/` only exercises a tiny synthetic resistor divider on
the default `local` backend — nothing there is heavy enough for backend
choice to actually matter, and nothing exercises `remote` at all. This
example fixes both gaps.

## Block: 31-stage sky130 ring oscillator

[`ring_tb.spice`](ring_tb.spice) — 31 `sky130_fd_pr__nfet_01v8`/
`sky130_fd_pr__pfet_01v8` inverter stages in a ring, a 20 fF load cap per
stage (to slow the ring and thicken the per-corner transient), and a narrow
`PULSE` kick source to start oscillation reliably regardless of corner.
Frequency varies strongly with process corner, so a real per-corner
`.meas`-based frequency measurement is meaningful, with wide sanity limits
(1 MHz–2 GHz) rather than a tight pass/fail band. Unlike `examples/sim/`,
this **does** depend on a real PDK (`sky130A`) — see "Regenerating" below.

**Approximation note.** The validation this example reproduces originally
measured a gf180 matrix; gf180 process-corner *bundles* are not yet
expressible in `klt sim` ([#351](https://github.com/2AMLogic/klayout-tools/issues/351)),
so both this example and the original validation run sky130A instead. The
elastic path being demonstrated (provision → push → parallel fan-out →
collect → teardown) is PDK-agnostic — the choice of PDK does not affect
what this example is meant to show.

## Requests

[`matrix-local.request.json`](matrix-local.request.json) and
[`matrix-remote.request.json`](matrix-remote.request.json) are otherwise
**identical**: the same 5-process-corner matrix (`tt`/`ss`/`ff`/`sf`/`fs` ×
1.8 V × 27°C), the same three measurements (`t_rise_400`, `t_rise_500`,
`ring_freq_hz`), differing only in `backend` and the addition of a `remote`
block on the remote variant. That's the point of the `backend` field
(docs/cli/sim.md's "Execution backends" table): a corner is a pure function
of netlist + models + corner, so both requests are expected to produce the
same report, just at different wall-clock cost.

`matrix-remote.request.json`'s `remote` block carries **documented
placeholder** values for the fields that are specific to one AWS account —
`key_name`, `ssh_key_path`, and `security_group_id` all read
`<your-...>` rather than a real keypair/security-group. Fill them in from
your own AWS setup before actually running it — see
[`docs/cli/sim.md`'s "Remote backend" field table](../../docs/cli/sim.md#remote-backend)
for what each field means and how it's resolved.

## Reference reports

| File | How it was produced |
| --- | --- |
| [`matrix-local.report.json`](matrix-local.report.json) | **Genuinely executed** in this environment on 2026-08-03: `uv run klt sim examples/sim-remote/matrix-local.request.json --format json`, against a real `sky130A` PDK install and `ngspice` 46. Every field — measurements, `runtime_s`, `environment` — is a live capture, not hand-edited. |
| [`matrix-remote.report.json`](matrix-remote.report.json) | **Hand-constructed, not a live `--backend remote` run** — this environment has no AWS credentials. Its corner `measurements` are copied byte-for-byte from `matrix-local.report.json` (per the reproducibility claim below); its `environment.remote` block (`instance_type`, `estimated_hourly_cost_usd`, `ami_id`, `pdk_snapshot`, `spin_up_s`) and per-corner `runtime_s` figures are derived from Epic #253's own closing-comment validation numbers (see "Measured comparison" below), scaled for illustration. `instance_id` is a placeholder, not a real AWS resource. Treat this file as a **schema illustration**, not evidence of an actual run. |

Both reports' `measurements`/`corners[].measurements` are identical except
for `runtime_s` and the `environment` block — exactly the additive-only,
same-corner-order guarantee `docs/cli/sim.md`'s "Execution backends"
section describes for every backend.

## Measured comparison

This repository's own regenerated `local` run (2026-08-03, this
environment — `ngspice` 46, `sky130A` via `~/.volare`):

| Corner | Ring freq (measured here) | Ring freq (Epic #253's original validation) |
| --- | --- | --- |
| `ss` | 123.498 MHz | 123.5 MHz |
| `sf` | 147.476 MHz | 147.5 MHz |
| `tt` | 169.217 MHz | 169.2 MHz |
| `fs` | 179.369 MHz | 179.4 MHz |
| `ff` | 218.207 MHz | 218.2 MHz |

The two runs — different machines, independently reconstructed netlists —
agree to 3 significant figures on every corner, which is itself a useful
confirmation that this example faithfully reproduces the original
validation's circuit.

Wall-clock and cost, however, are **not** independently reproduced here —
only `local` was actually run in this environment (no AWS credentials
available to run `remote`). The figures below are Epic #253's own,
quoted from [its closing comment](https://github.com/2AMLogic/klayout-tools/issues/253#issuecomment-5167117967):

| Backend | Wall-clock | Verdict | Notes |
| --- | --- | --- | --- |
| `local` (sequential, Epic #253's own M-series laptop) | **11m 42s** | pass 5/5 | ~2.3 min/corner of `ngspice`, run sequentially |
| `remote` (`c7i.12xlarge` on-demand, `us-west-2`) | **4m 39s** | pass 5/5, **identical to the decimal** vs. `local` | 2.5× wall-clock win; `spin_up_s: 19.0` |
| This repo's own regenerated `local` run (2026-08-03) | ~36 min (sum of per-corner `runtime_s`) | pass 5/5 | Slower per-corner than Epic #253's laptop — a different, presumably less powerful machine; the *relative* backend comparison is unaffected since only `local` ran here |

**Cost.** Epic #253's `remote` run resolved an estimated
`$2.142`/hour on-demand `c7i.12xlarge`; at 4m 39s (0.0775 h) that's
**≈ $0.16** for the entire 5-corner matrix — the cost figure the
`remote` backend's `max_hourly_cost_usd` guardrail (docs/cli/sim.md) is
designed to make visible up front, not discover after the bill arrives.

**Caveat: environment-dependent behavior.** The validation that produced
these historical numbers also surfaced several rough edges specific to
live AWS conditions — spot-capacity availability, SSH reachability behind
NAT/multi-egress networks, first-boot cold-start timing — that are
inherently environment-dependent and won't reproduce identically on a
different account, region, or network path. See
[#371](https://github.com/2AMLogic/klayout-tools/issues/371) for the
field-hardening that followed from that run (SSH-ready default,
spot-to-on-demand fallback, multi-`launcher_cidr` support). Don't expect
`matrix-remote.request.json` to "just work" against an arbitrary AWS
account/network without adapting these fields to your own environment.

## Regenerating

```sh
uv run klt sim examples/sim-remote/matrix-local.request.json --format json \
    > examples/sim-remote/matrix-local.report.json
```

Requires a resolvable `sky130A` PDK install (`klt pdk find --pdk sky130A`)
and `ngspice` on `PATH` — unlike `examples/sim/`'s synthetic fixtures, this
is a real PDK deck, and a **heavy** one: each corner is a 12 µs transient
at `reltol=1e-4` across 31 stages, taking anywhere from a few minutes to
several minutes per corner depending on the machine (the whole 5-corner
matrix took about 36 minutes on the machine this reference report was
captured on). `runtime_s` and `environment.engine_version`/`models_lib*`
are machine-dependent and will not reproduce byte-for-byte, but the
pass/fail verdicts and per-corner measured values should.

`matrix-remote.report.json` is **not** mechanically regenerable from this
checkout — actually running `--backend remote` needs a configured AWS
account (region, EC2 keypair, security group or CIDR — see
`docs/cli/sim.md`'s field table) with real values in place of
`matrix-remote.request.json`'s placeholders, and incurs real (if small,
~$0.16-per-run) AWS cost. It is committed here as a schema/shape
illustration only (see "Reference reports" above).
