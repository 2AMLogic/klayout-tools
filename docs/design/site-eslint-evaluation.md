# Evaluation: ESLint (React hooks + explicit `any`) for `site/`

**Status:** evaluation / recommendation (issue
[#6746](https://github.com/2AMLogic/klayout-tools/issues/6746)). Report-only:
nothing here installs a linter, adds a CI gate, or changes application code.
It is independent of the comment-only cleanup in
[#5732](https://github.com/2AMLogic/klayout-tools/issues/5732), whose
[recorded decision](https://github.com/2AMLogic/klayout-tools/issues/5732#issuecomment-6989408647)
asked for this assessment separately.

**Forcing function:** the `site` CI job
(`.github/workflows/ci.yml`, "site/ (tsc + vitest)") runs `tsc -b` and
`vitest run`. Neither checks React hook dependency arrays or reports explicit
`any`. Five `eslint-disable-next-line` directives in `site/src` nevertheless
name rules from a linter the site has never had. The question is whether the
two rules those directives name would pay for themselves if the site actually
adopted ESLint.

**Everything below was run, not recalled.** The trial ran on 2026-11-18
against `site/` at commit `94950eeee19e4219d938b81cf742065bd333d750` (the
tip of `main` when the trial started), in a throwaway directory outside the
repository. Nothing was installed into the repository, the host, or a global
location.

## 1. Recommendation

**Do not adopt ESLint for `site/` now. Re-evaluate when `typescript-eslint`
supports the TypeScript the site is on, or when the site's hook surface grows.**

- **Yield on today's tree is near zero.** Across 66 files / 10,697 lines the
  two rules produce 5 findings plus 1 inert directive (§4). None is a
  defect. One is a low-impact quality finding. Two are intentional effect
  triggers where applying the rule's own suggested fix breaks 8 tests (§5).
  Two are canvas test doubles whose casts can be written without `any`.
- **The cost is the dependency shape, not CI minutes.** The site is on
  TypeScript 7.0.2, which `typescript-eslint` 8.73.0 refuses to load. Adoption
  needs a TypeScript 6/7 side-by-side alias, +131 locked packages, and a
  suppression discipline that already drifted once (the five directives that
  prompted this issue). The CI step itself is about 2 s (§6).
- **The plugin's default preset would add noise, not signal.**
  `eslint-plugin-react-hooks` 7.1.1's `recommended-latest` preset reports 7
  more findings on this tree (9 errors in total) from React-Compiler-derived
  rules (§5). An adoption has to pin two explicit rules and keep them pinned.

**What would change the answer:**

1. `typescript-eslint` supports the TypeScript API the site uses. Upstream
   tracks this in
   [typescript-eslint#10940](https://github.com/typescript-eslint/typescript-eslint/issues/10940),
   and the TypeScript 7.0 announcement says 7.1 is expected to ship a new API.
   That removes the alias.
2. The site's stateful viewers (`field/`, `waveform/`, `layout/`) multiply.
   The value of `exhaustive-deps` is preventive: it guards hooks that do not
   exist yet. Five non-test source files use effect or memo hooks today (seven
   use any React hook), which is little surface to amortize the setup.
3. The project wants the two rules sooner and accepts a different engine (§8).

If the decision is to adopt anyway, the minimal change set is in §7.

## 2. Trial setup (reproducible)

| Item | Version |
| --- | --- |
| Named commit | `94950eeee19e4219d938b81cf742065bd333d750` |
| Node / npm | 24.22.1 / 11.19.1 (CI uses Node 24) |
| `eslint` | 10.15.0 |
| `typescript-eslint` | 8.73.0 (`@typescript-eslint/parser` 8.73.0) |
| `eslint-plugin-react-hooks` | 7.1.1 |
| Site `typescript` (as shipped) | 7.0.2 |
| Linter-side TypeScript | `@typescript/typescript6` → 6.0.2 |
| `oxlint` (probe, §8) | 1.94.0 |

The trial used the versions npm resolved on 2026-11-18. The block below pins
the direct trial packages. npm resolves their transitive dependencies at install
time, so the package count can vary on a later run.

```bash
SHA=94950eeee19e4219d938b81cf742065bd333d750
TRIAL=$(mktemp -d)
git archive "$SHA" site blocks | tar -x -C "$TRIAL"   # blocks/ is read by one site test
cd "$TRIAL/site"
npm ci --ignore-scripts --no-audit --no-fund           # baseline: 194 locked packages

# (a) Naive adoption -> npm ERESOLVE (see section 3)
npm install -D eslint@10.15.0 typescript-eslint@8.73.0 eslint-plugin-react-hooks@7.1.1

# (b) Forced (--legacy-peer-deps) -> installs, then fails to load (see section 3)

# (c) The supported side-by-side layout (the one the results below use)
npm install -D --ignore-scripts --no-audit --no-fund \
  "typescript@npm:@typescript/typescript6@6.0.2" \
  "@typescript/native@npm:typescript@7.0.2" \
  eslint@10.15.0 typescript-eslint@8.73.0 eslint-plugin-react-hooks@7.1.1
```

`site/eslint.config.js` for the trial, with exactly the two rules the issue
names:

```js
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { parser: tseslint.parser },
    plugins: { "react-hooks": reactHooks, "@typescript-eslint": tseslint.plugin },
    rules: {
      "react-hooks/exhaustive-deps": "warn",
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
];
```

The runs, all from `$TRIAL/site`:

```bash
npx eslint src -f json                          # A: existing directives honored
npx eslint src --no-inline-config -f json       # B: what the directives hide
```

A second config replaced the `react-hooks` plugin entry with
`reactHooks.configs.flat["recommended-latest"]` to measure the default
preset (§5). Everything else was identical.

## 3. Result 1: the site's TypeScript blocks a plain install

The site depends on `typescript@^7.0.2`. `typescript-eslint` 8.73.0 declares
`typescript: >=4.8.4 <6.1.0`.

| Path | Outcome |
| --- | --- |
| (a) `npm install -D eslint typescript-eslint eslint-plugin-react-hooks` | **`ERESOLVE`**: `peer typescript@">=4.8.4 <6.1.0" from typescript-eslint@8.73.0` vs `typescript@7.0.2`. No `package.json` or lockfile change is produced. |
| (b) the same with `--legacy-peer-deps` | Installs, then every `eslint` run dies at load: `Error: typescript-eslint does not support TS 7.0.` TypeScript 7.0 ships no programmatic API (`require('typescript')` exposes 2 keys and no `createSourceFile`). |
| (c) side-by-side aliases (the layout in §2) | Works. The linter gets the TypeScript 6.0.2 API and `tsc` stays 7.0.2. |

Path (c) is the route the TypeScript 7.0 announcement recommends ("Running
Side-by-Side with TypeScript 6.0") and the one `typescript-eslint`'s own error
message points to. Under (c), with `blocks/` present, `npm run check`
(`tsc -b`) passes and the full vitest suite passes (22 files, 257 tests). Run
without `blocks/` in the trial copy, one test fails because it reads
`blocks/<slug>/output/<file>` from the repository root. That is an artifact
of archiving only `site/`, not of the alias layout.

The alias is a temporary workaround with a known expiry (upstream support for
the TypeScript 7.1 API), but it means the site's `typescript` entry would no
longer name the compiler that `tsc` runs.

## 4. Result 2: findings by rule

Two rules, `site/src` at `94950eeee`, 66 files.

| Run | `react-hooks/exhaustive-deps` | `@typescript-eslint/no-explicit-any` | Other |
| --- | --- | --- | --- |
| **A**: existing directives honored | 1 (warn) | 0 | 1 unused-directive warning |
| **B**: `--no-inline-config` | 3 (warn) | 2 (error) | 0 |

ESLint 10's flat config reports an unused `eslint-disable` directive by
default, which is how run A surfaced the stale one with no extra flag. With the
default severities above, run A exits 0 (warnings only) and run B exits 1.

## 5. Result 3: representative findings, actionable vs intentional

Run B, all findings, classified by reading the code at each site.

| # | Site | Rule | Existing directive | Verdict |
| --- | --- | --- | --- | --- |
| 1 | `waveform/WaveformViewer.tsx:230` deps `[liveWaveform]`, missing `allNodes` | `exhaustive-deps` | line 229 | **Intentional trigger.** The effect seeds the overlay when a new live waveform arrives. Adding `allNodes` would re-run it whenever the static node set changes. |
| 2 | `waveform/WaveformViewer.tsx:258` deps `[selectedCorners, slug]`, missing `cornerIndex`, `displayCorners`, `waveforms` | `exhaustive-deps` | line 257 | **Intentional trigger, and the rule's fix is harmful.** The effect writes `waveforms`. Making it a dependency restarts the effect on every cache write, and the cleanup then discards the in-flight fetch. |
| 3 | `waveform/WaveformViewer.tsx:292` `nodesToShow` feeding `useMemo` at line 323 | `exhaustive-deps` | none | **Actionable, low impact.** `selectedNodes ?? new Set(allNodes)` is a fresh `Set` every render while `selectedNodes` is `null`, so that `useMemo` never caches in that state. The effect at line 283 sets `selectedNodes` as soon as `allNodes` is non-empty, and while it is empty no waveform is loaded and the memo body loops over nothing. No observable effect. |
| 4 | `layout/GdsCanvas.test.tsx:220` `as any` | `no-explicit-any` | line 219 | **Canvas test double.** Stands a recording 2D context in for the overloaded `getContext`. |
| 5 | `test-utils/mocks.ts:52` `as any` | `no-explicit-any` | line 51 | **Canvas test double.** The same overload, for the WebGL fake. |
| – | `field/FieldViewer.tsx:279` deps `[mesh, frame, triangleIndices, orbit, bounds, width, height]` | `exhaustive-deps` | line 278 | **No finding.** The directive suppresses nothing. The rule already accepts this array, so the directive was inert even for a hypothetical linter. |

Two of those claims were tested rather than argued, in the disposable copy
only:

- **Finding 2's suggested fix.** Adding `cornerIndex, displayCorners,
  waveforms` to the `:258` dependency array makes **8** tests in
  `WaveformViewer.test.tsx` fail. The observed symptom is that the plot never
  renders (`Unable to find an element by: [data-testid="waveform-plot"]`).
  The unmodified file passes 36/36 across the two waveform test files. The
  mechanism below is from reading the code, not separately instrumented: the
  effect writes `waveforms` (`"loading"`), the changed dependency re-runs it,
  the previous run's cleanup sets `cancelled = true` so its fetch result is
  dropped, and the re-run skips the corner because it is already marked
  loading. Following the rule here would introduce a bug. Silencing it, with a
  stated reason, is the correct outcome.
- **Findings 4 and 5.** Changing both casts from `as any` to `as never` passes
  `tsc -b` and the 16 `GdsCanvas` tests. These sites are fixable without
  `any`. The alternative is a per-file override for tests, which costs a
  config block instead of two edits.

The default preset is much noisier. Replacing the two named rules'
`react-hooks` entry with `reactHooks.configs.flat["recommended-latest"]`
reports **12 findings, 9 of them errors**, with the existing directives ignored
(run B′), and 9 findings (7 errors) with them honored (run A′):

| Rule (run B′) | Count |
| --- | --- |
| `react-hooks/set-state-in-effect` | 6 (`FieldViewer.tsx:171`, `GdsCanvas.tsx:180`, `GdsCanvas.tsx:241`, `WaveformViewer.tsx:219`, `:238`, `:285`) |
| `react-hooks/exhaustive-deps` | 3 (as above) |
| `@typescript-eslint/no-explicit-any` | 2 (as above) |
| `react-hooks/preserve-manual-memoization` | 1 (`WaveformViewer.tsx:268`) |

These 7 extra findings are outside the issue's two-rule scope and were
**not triaged**. They show only that wholesale adoption of the plugin preset
would start red.

## 6. Cost of another CI check

Measured on this shared 8-vCPU host with a warm npm cache and
`--ignore-scripts`, so the numbers are indicative, not a CI benchmark.

| Cost | Measured |
| --- | --- |
| Lint run, 66 files, two rules, no type-aware linting | 1.8-2.0 s wall, about 400 MB peak RSS |
| `npm ci` | 2.3 s (baseline) → 3.2 s (with the linter lockfile) |
| Locked packages | 194 → 325 (+131) |
| `node_modules` | 176 MB → 258 MB (+82 MB) |
| `site` job budget | `timeout-minutes: 15`, so the step is negligible against it |

CI minutes are not the objection. The maintenance cost is:

- **Dependency churn.** Dependabot already covers `/site` npm weekly with the
  `npm-minor-patch` group and a 14-day cooldown (`.github/dependabot.yml`).
  No config change is needed. The trial added four direct dependencies and
  changed the existing `typescript` entry to an alias. Whether Dependabot updates an `npm:`-aliased
  `typescript` entry correctly was **not verified** here.
- **The deploy path installs it too.** `scripts/deploy-site.sh` runs `npm ci`,
  so the linter packages would also be installed on every deploy.
- **Suppression discipline.** Findings 1 and 2 are correct code that the rule
  flags. Each needs a directive that actually exists and says why. That is
  the exact drift that produced #5732: directives naming a linter nothing ran.
  A gate makes the directives load-bearing, which is better, but it is a
  standing obligation.

## 7. If adopted anyway: the minimal change set

1. `site/package.json`: set `typescript` to
   `npm:@typescript/typescript6@^6.0.2`, add `@typescript/native` as
   `npm:typescript@^7.0.2` (so `npx tsc` stays 7.0.x), add `eslint`,
   `typescript-eslint` and `eslint-plugin-react-hooks` to `devDependencies`,
   add the script `"lint": "eslint src --max-warnings 0"`, and regenerate
   `package-lock.json`. Drop the alias once `typescript-eslint` supports the
   site's TypeScript.
2. `site/eslint.config.js`: the 15-line config in §2. Enable the two rules
   explicitly rather than extending a preset (§5). Either change the two casts
   to `as never` or add a test-files override for
   `no-explicit-any`.
3. `.github/workflows/ci.yml`, `site` job: one step after "Type check",
   `npm --prefix site run lint`, under the existing
   `steps.filter.outputs.site_changed == 'true'` condition. No new job, no
   change to Node setup. The existing cache configuration uses the same
   lockfile path and picks up its new content hash.
4. Before the gate goes on: fix or explain finding 3, and note that
   `exhaustive-deps` is `warn` and does not fail a run unless the script
   passes `--max-warnings 0` (run A exited 0 with two warnings).
5. Ordering with #5732: that issue replaces the five directives with comments.
   If ESLint is adopted afterwards, findings 1 and 2 need directives again
   (with the reason after `--`), while `FieldViewer.tsx:278` does not. #5732's
   comment for that site can describe the effect's triggers without claiming
   an omitted dependency.

## 8. Alternative probe: oxlint (not an adoption proposal)

Because the blocker in §3 comes from the TypeScript-parser dependency, a
probe checked whether another engine gets the same two rules without it.
`oxlint` 1.94.0 is a single native binary with its own parser.

| | ESLint 10 (§2 layout) | oxlint 1.94.0 |
| --- | --- | --- |
| Packages installed | +131 locked | 2 |
| Disk | +82 MB | 18 MB |
| Wall time, 66 files | about 1.9 s | 0.04-0.05 s |
| TypeScript peer constraint | `<6.1.0` (blocks TS 7) | none |
| Findings, directives stripped | 5 | 5 (same sites) |
| `FieldViewer.tsx:278` | no finding | no finding |

The probe ran `oxlint --react-plugin -A all -D react/exhaustive-deps -D
typescript/no-explicit-any src`. oxlint has no "ignore inline directives"
flag, so the 5-finding row ran on a copy of `src` with the directive lines
deleted. With directives honored it reported 1 finding (the `nodesToShow`
memo, reported at the `useMemo` call site, line 323).

This is a probe only. No oxlint config, CI wiring, rule-parity audit beyond
these two rules, or Dependabot behavior was evaluated, and it is not the tool
the issue asks about. If the project wants these rules before `typescript-eslint`
catches up, this is the cheaper candidate and deserves its own decision.

## 9. What was not verified

- **GitHub Actions timing.** Timings are from a shared host with a warm cache
  and `--ignore-scripts`. Native-binary install scripts, cold caches and the
  hosted runner will differ.
- **Dependabot and aliases.** Not checked (§6).
- **Findings classification.** It comes from reading the code at each site,
  plus the two experiments in §5. The seven preset-only findings were not
  triaged.
- **Type-aware linting** (`recommendedTypeChecked` and the like) was out of
  scope and not run. It would add a TypeScript-program build to every lint
  run.
- **No live-verification step applies.** Nothing here depends on a live
  service or credential, and the trial is fully reproducible from §2.
