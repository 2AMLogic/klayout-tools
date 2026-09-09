# RTL review guide: protocol & clock domain crossings

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/rtl/protocol-cdc.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary and its machine-parsed findings-JSON schema into this repo's
> `klt` verbs, request/response fields, and plain PR-review-comment
> convention; the severity/confidence contract and the checklist content
> carry over unchanged. See [`README.md`](README.md) for when this guide
> applies and which `klt` verb (if any) supplies the tool evidence it asks
> a reviewer to check.

This guide is for whoever reviews an RTL pull request for **interface
protocol violations and clock domain crossing issues**. Scope is exactly
these two classes — internal functional logic, style, security, and
optimizations belong to the sibling guides in [the index](README.md).

## Scope boundaries

- **Internal FSM deadlocks** (not involving module port protocols): defer
  to [`rtl-bugs.md`](rtl-bugs.md). Focus here on deadlocks at the
  interface boundary (e.g., valid asserted but ready never comes).
- **General unused/dead RTL** (not protocol-related): defer to
  [`rtl-optimization.md`](rtl-optimization.md). Only flag an unused port
  here when it is part of a protocol and indicates missing or broken
  protocol behavior (e.g., a `ready` output never driven, a `valid` input
  never sampled).

## Procedure

1. Read every RTL file the PR changes.
2. Read package/include files those files reference when they define
   types, parameters, macros, or interfaces needed to understand the
   change.
3. Identify all module ports and their protocols (ready/valid,
   request/acknowledge, memory interface, etc.).
4. Count distinct clock inputs. If single-clock and no async external
   inputs, skip the CDC section.
5. Review against the checklist below.
6. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and tagged with the severity and confidence levels
   below.

## Severity and confidence

Severity heuristic:
- **CRITICAL** — protocol bug causing data loss or corruption, or
  unsynchronized CDC causing metastability.
- **MAJOR** — protocol issue that may cause stall/deadlock under specific
  conditions, or CDC concern with low probability of failure.

Confidence:
- **HIGH** — definitely a violation based on the code alone.
- **MEDIUM** — likely a violation but depends on how the module is
  instantiated/used.
- **LOW** — suspicious pattern; may be handled by the instantiating
  module.

**Quality over quantity:** prefer fewer, higher-confidence findings over
many speculative ones. Do not flag something CRITICAL with LOW confidence.
If unsure whether a protocol is violated, explain the assumption and use
LOW confidence.

---

## Checklist

### A. Handshake & protocol (CRITICAL/MAJOR)

- **Ready/valid violations** (CRITICAL if data loss): Data must be stable
  while valid is asserted and ready is low. Valid must not depend
  combinationally on ready (creates a combinational loop through the
  interconnect). Check both directions. `klt functional-verification`'s
  own pass/fail (`status`, `tests[]`) is the mechanical backstop for a
  handshake bug a directed test actually exercises — see the index's
  evidence table for the field mapping — but this review step exists
  because a weak testbench will not exercise every handshake path.
- **Liveness / progress**: Can the interface deadlock? Is eventual
  transfer guaranteed, or can the module stall indefinitely waiting for a
  condition that never occurs? Trace all paths from valid-asserted to
  transfer-complete.
- **Backpressure handling**: What happens when a downstream consumer
  deasserts ready for an extended period? Can the module handle it without
  data loss, buffer overflow, or state corruption?
- **Request/acknowledge protocols**: For non-ready/valid interfaces,
  verify the handshake sequence is correct. Check that the module does not
  drop requests or double-acknowledge.
- **Memory interface protocols**: For read/write interfaces, verify
  address stability during transactions, correct handling of wait states,
  and proper read data sampling timing.

### B. Port & signal issues (MAJOR)

- **Unused protocol ports**: Protocol ports declared but never read
  internally (dead inputs) or driven but never connected externally (dead
  outputs). Report here only when this indicates missing or broken
  protocol behavior; stale, behavior-neutral interfaces belong to
  [`rtl-optimization.md`](rtl-optimization.md).
- **Signal stability / glitches**: Control outputs that are combinational
  functions of multiple changing inputs. These can glitch and cause issues
  in downstream modules. Outputs crossing module boundaries should
  generally be registered.
- **Output contention**: Multiple modules driving the same signal through
  a shared bus without proper arbitration or tri-state control.

### C. Clock domain crossings (CRITICAL/MAJOR)

*Skip this section if the module has only one clock input and no
asynchronous external inputs. Do not emit a finding only to say CDC was
skipped.*

- **Missing synchronizers** (CRITICAL): Signals sampled in a clock domain
  different from where they are driven, without a 2-FF synchronizer or
  handshake protocol.
- **Multi-bit CDC** (CRITICAL): Multi-bit buses crossing clock domains
  without gray coding, MCP formulation, or a handshake/FIFO.
- **Reset domain crossings**: Async reset deassertion not synchronized to
  the destination clock domain.
- **Async external inputs**: Even in single-clock modules, external
  asynchronous inputs (interrupts, test pins, external status signals)
  require synchronization before use in sequential logic.
- **Generated clocks**: Combinational logic used to generate clock signals
  (clock gating without proper cells, divided clocks from counters used as
  clock inputs).
