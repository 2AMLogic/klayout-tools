# RTL review guide: security

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/rtl/security.md`
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

This guide applies when reviewing RTL for **security-sensitive IP**
(cryptographic, safety-critical, or any design handling secrets) — side
channel leakage, data exposure, fault injection weaknesses, and error
handling gaps. General functional bugs, style, and optimizations are out
of scope — see the sibling guides in [the index](README.md). `sky130-modexp`
is the canary this guide is most directly relevant to today.

## Procedure

1. Read every RTL file the PR changes.
2. Read package/include files those files reference when they define
   types, parameters, macros, or interfaces needed to understand the
   change.
3. Determine if the changed module handles secret or sensitive data (keys,
   intermediate protected values, random numbers). If the module is
   clearly non-security-relevant (e.g., a generic FIFO, address decoder,
   clock divider), skip this guide and note in the review that no
   security-relevant checklist applies.
4. Review against the checklist below.
5. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and tagged with the severity and confidence levels
   below.

## Severity and confidence

Severity heuristic:
- **CRITICAL** — exploitable side channel, secret data leakage, or
  security bypass. Would cause certification failure (e.g., FIPS, Common
  Criteria, or equivalent).
- **MAJOR** — defense-in-depth weakness. Single point of failure that
  could be exploited with fault injection, or missing redundancy in a
  security check.

Confidence:
- **HIGH** — definitely a vulnerability based on the code alone.
- **MEDIUM** — likely a vulnerability but depends on threat model or
  system-level context.
- **LOW** — suspicious pattern; may be mitigated elsewhere in the design.

**Quality over quantity:** prefer fewer, higher-confidence findings over
many speculative ones. Do not flag something CRITICAL with LOW confidence.
If unsure whether a pattern is exploitable, use LOW confidence and explain
the conditions under which it would matter.

---

## Checklist

### A. Constant-time violations (CRITICAL)

- **Data-dependent branching**: `if`/`case` statements where the condition
  depends on secret data (keys, plaintext, intermediate sensitive values).
  The branch taken must not vary with secret values.
- **Variable-latency operations**: Loops where the iteration count depends
  on secret data, early-exit conditions based on secret comparisons.
- **Data-dependent memory access patterns**: Array indexing where the
  index depends on secret data (enables cache-timing attacks in software,
  power analysis in hardware).
- **Timing variation through muxing**: Different-length combinational
  paths selected by a secret-dependent mux.

### B. Data leakage (CRITICAL)

- **Secrets left in registers**: After an operation completes, are
  intermediate values (keys, nonces, partial results) zeroized? Check that
  the module clears sensitive state on completion or error.
- **Secrets on observable outputs**: Are internal secret values exposed on
  debug ports, status registers, or error messages?
- **Secrets surviving reset**: After a key-update or secure-wipe sequence,
  can any secret data be recovered from registers or memory?

### C. Fault injection exposure (MAJOR)

- **Unprotected security checks**: A single comparison (`if (tag_valid)`)
  that, if flipped by a fault, bypasses authentication or validation.
  Critical checks should be redundant (checked twice, or protected by
  error-detecting encoding).
- **FSM single-bit vulnerability**: Can a single bit-flip in the FSM state
  register move the module from a locked/error state to an operational
  state? State encoding should make this impossible (e.g., Hamming
  distance > 1 between security-critical states).
- **Counter/loop skip**: Can a fault on a loop counter cause a
  security-critical operation to skip rounds (e.g., pipeline stages,
  iterative computations)?

### D. Error handling & safe state (CRITICAL if exploitable, MAJOR
otherwise)

- **Missing error detection**: Are all illegal states, invalid inputs, and
  memory faults detected?
- **Unsafe error response**: On detecting an error, does the module
  transition to a safe/locked state, or does it silently continue with
  corrupted data?
- **Error recovery leaks**: Can an error condition be used to extract
  partial secret data (e.g., differential fault analysis)?
- **Incomplete error coverage**: Are there error conditions that are
  checked in some paths but not others?
