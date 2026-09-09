# RTL review guide: code style & assertions

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/rtl/code_style.md`
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

This guide is for whoever reviews an RTL pull request for **code style:
comments, naming, readability, and assertion coverage**. Functional
correctness, synthesis, security, and conditional compilation are out of
scope — see the sibling guides in [the index](README.md).

## Procedure

1. Read every RTL file the PR changes.
2. Read package/include files those files reference when they define
   names, types, parameters, macros, or interfaces needed to understand
   the change.
3. Read [`rtl-style-guide.md`](rtl-style-guide.md).
4. Review against **all rules** in every section of the style guide, using
   the severity levels specified there.
5. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and tagged with the confidence level below plus the
   style guide's own per-rule severity.

## Confidence

- **HIGH** — objectively incorrect (wrong comment, clearly misleading
  name).
- **MEDIUM** — subjective but most reviewers would agree.
- **LOW** — style preference; may be intentional.

**Quality over quantity:** prefer fewer, higher-confidence findings.
Project-specific conventions (a canary's own `CLAUDE.md`) override general
best practices.
