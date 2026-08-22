/**
 * In-browser live-solve capability check (Epic #840 Phase 2c, issue #891).
 *
 * The Electromagnetics gallery (`components/em/EmGallerySection.tsx`, née
 * the headline "Electromagnetics" gallery of Phase 1c/#851) ships **only**
 * Phase 1's pre-computed geode-fem exports today — there is no live-solve
 * UI in this repo yet (Phase 2's 2a/2b/2d, issues #889/#890/#892, are still
 * unbuilt). This module exists so that whichever live-solve interaction
 * eventually lands has a capability gate to call *before* it does anything
 * expensive, and so the gallery can show a clear, non-alarming "live
 * solving isn't available here" note in the meantime — see
 * `components/em/LiveSolveAvailabilityNotice.tsx` for the wiring.
 *
 * **What this checks, and why it is not simply `"gpu" in navigator`:**
 * [`docs/design/geode-fem-wasm-webgpu-spike.md`](../../../docs/design/geode-fem-wasm-webgpu-spike.md)
 * (issue #889, merged via PR #968) measured, with a working WASM build, that:
 *
 *   - The patch-antenna benchmark's solve reproduces the committed Phase-1
 *     oracle to 6.8e-14 relative error running on the **CPU inside plain
 *     WebAssembly** (`wasm32-unknown-unknown`) — no WebGPU involved (§3).
 *   - **WebGPU cannot carry the result**: the solve's dominant cost is a
 *     host-side `faer` sparse-LU factorization that never touches a
 *     Burn/wgpu tensor backend (§4.2), and WebGPU has no `f64` type at all
 *     in WGSL — `wgpu::Features::SHADER_F64` is documented as
 *     *"native only"* and no browser adapter exposes it — while geode-fem's
 *     oracles are f64-calibrated (§4.3).
 *   - The real hazard for a browser solve is **memory**, not GPU access:
 *     the measured `wasm32` linear-memory high-water mark for one
 *     frequency-point solve is ~610 MiB (§5), which the spike itself flags
 *     as "a caution ... a tab OOM is a plausible failure mode on low-memory
 *     devices and would need a capability check" (§10).
 *
 * So `checkLiveSolveCapability()`'s overall {@link LiveSolveCapabilityResult.supported}
 * verdict gates on **WebAssembly support and a memory heuristic**, not on
 * WebGPU presence — WebGPU is still probed and reported (`checks.webGpu`)
 * because the epic is framed around it and a future solver build may want
 * it, but per the spike it neither gates nor unlocks today's only proven
 * solve path (CPU/WASM). A future live-solve UI that *does* pick a
 * WebGPU-dependent path can additionally require `checks.webGpu` itself;
 * this module does not hide that signal, it just doesn't presume it's
 * load-bearing.
 *
 * **Cheap by construction (acceptance criterion: no wasted worker/solve
 * resources).** Every check here is a capability *query*: `"gpu" in
 * navigator` plus `GPUAdapter.requestAdapter()` (queries hardware
 * capability; does **not** reserve a GPU context — this module deliberately
 * never calls `requestDevice()`), `typeof WebAssembly`, and
 * `navigator.deviceMemory` (a static property read). Nothing here
 * instantiates a `Worker`, fetches a `.wasm` module, or runs any solve.
 */

/** Guards the ~610 MiB `wasm32` linear-memory high-water mark the spike
 *  measured for one frequency-point solve (§5) against `navigator.deviceMemory`
 *  (reported in whole GiB, rounded down per spec) — 2 GiB leaves headroom
 *  below the measured figure only for a comfortably-provisioned device, so
 *  this asks for the next tier up. */
export const MIN_DEVICE_MEMORY_GIB = 4;

export interface LiveSolveCapabilityChecks {
  /** `WebAssembly` global present. A hard requirement of every solve path
   *  the spike measured (§3) — without it there is nothing to gate. */
  webAssembly: boolean;
  /** `navigator.gpu` present and `requestAdapter()` resolved to a non-null
   *  adapter. Reported for forward-compatibility with a future
   *  WebGPU-dependent solve path; per the spike (§4) it does not affect
   *  {@link LiveSolveCapabilityResult.supported} today — see module doc. */
  webGpu: boolean;
  /** `navigator.deviceMemory >= MIN_DEVICE_MEMORY_GIB`. `null` when the
   *  browser doesn't expose `navigator.deviceMemory` (Safari never does) —
   *  treated as "unknown", not "insufficient": failing every Safari visitor
   *  on missing telemetry would be worse than the OOM risk this guards
   *  against. */
  sufficientMemory: boolean | null;
}

export interface LiveSolveCapabilityResult {
  /** Overall verdict: true only when every check this module treats as a
   *  hard requirement passes (`webAssembly`, and `sufficientMemory` is not
   *  explicitly `false`). See module doc for why `webGpu` is excluded. */
  supported: boolean;
  /** Human-readable reasons `supported` is `false`, most specific first —
   *  for developer-facing diagnostics/logging, not verbatim end-user copy
   *  (the UI notice uses its own non-alarming wording). Empty when
   *  `supported` is `true`. */
  reasons: string[];
  checks: LiveSolveCapabilityChecks;
}

interface NavigatorGpuLike {
  gpu?: {
    requestAdapter: (options?: unknown) => Promise<unknown | null>;
  };
  deviceMemory?: number;
}

/**
 * Run the capability probe. Safe to call from any environment (SSR/prerender
 * included — see `scripts/prerender.mjs`): falls back to "unsupported, no
 * `navigator`" rather than throwing when `navigator`/`WebAssembly` are
 * undefined.
 */
export async function checkLiveSolveCapability(): Promise<LiveSolveCapabilityResult> {
  const reasons: string[] = [];

  const webAssembly = typeof WebAssembly !== "undefined";
  if (!webAssembly) {
    reasons.push("WebAssembly is not available in this browser.");
  }

  const nav: NavigatorGpuLike | undefined =
    typeof navigator !== "undefined" ? (navigator as unknown as NavigatorGpuLike) : undefined;

  const webGpu = await probeWebGpu(nav);

  const sufficientMemory = probeMemory(nav);
  if (sufficientMemory === false) {
    reasons.push(
      `This device reports less than ${MIN_DEVICE_MEMORY_GIB} GiB of memory, below what an in-browser ` +
        "solve needs to avoid an out-of-memory failure.",
    );
  }

  return {
    supported: webAssembly && sufficientMemory !== false,
    reasons,
    checks: { webAssembly, webGpu, sufficientMemory },
  };
}

/**
 * `navigator.gpu.requestAdapter()` only — never `GPUAdapter.requestDevice()`.
 * `requestAdapter` is a capability query (does it exist, what can it do);
 * `requestDevice` reserves a real GPU context, which this capability check
 * must never do (acceptance criterion: no wasted resources on a check).
 */
async function probeWebGpu(nav: NavigatorGpuLike | undefined): Promise<boolean> {
  if (!nav || !nav.gpu || typeof nav.gpu.requestAdapter !== "function") return false;
  try {
    const adapter = await nav.gpu.requestAdapter();
    return adapter != null;
  } catch {
    // A throwing requestAdapter() (seen on some locked-down/headless
    // configurations) is a "not available" signal, not a hard failure.
    return false;
  }
}

function probeMemory(nav: NavigatorGpuLike | undefined): boolean | null {
  const deviceMemory = nav?.deviceMemory;
  if (typeof deviceMemory !== "number" || Number.isNaN(deviceMemory)) return null;
  return deviceMemory >= MIN_DEVICE_MEMORY_GIB;
}
