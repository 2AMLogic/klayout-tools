import { useEffect, useState } from "react";
import { checkLiveSolveCapability, type LiveSolveCapabilityResult } from "@/lib/liveSolveCapability";

/**
 * Graceful-fallback notice for a future in-browser live solve (Epic #840
 * Phase 2c, issue #891): runs {@link checkLiveSolveCapability} once on
 * mount and, only when the browser can't support an in-browser solve,
 * renders a small, non-alarming inline note pointing at the pre-computed
 * results instead.
 *
 * **NOT MOUNTED ANYWHERE YET — this is deliberate, not an oversight.**
 * There is no live-solve UI in this repo (Phase 2a/2b/2d — issues
 * #889/#890/#892 — are unbuilt; see
 * `docs/design/geode-fem-wasm-webgpu-spike.md`'s DEFER verdict), so
 * `EmGallerySection`'s benchmark entries (`PatchAntennaGalleryEntry` etc.)
 * always render Phase 1's pre-computed results regardless of what this
 * component would find — the "fallback" the acceptance criteria asks for is
 * already that section's only rendering path. Rendering this notice on the
 * public site today would therefore tell a real visitor that live solving
 * is unavailable *on their device* when it is unavailable on every device,
 * and `navigator.deviceMemory < 4` is a routinely-hit condition on real
 * mobile traffic — not a hypothetical. So the probe and this notice ship
 * built, exported, and tested, and the component gets mounted by whichever
 * component owns the live-solve entry point once #890/#892 land, at which
 * point the copy below is accurate. `EmGallerySection.test.tsx` asserts it
 * stays unmounted until then.
 *
 * When the check finds the browser capable, this renders nothing — a
 * "you *could* run this" note is noise next to a working live-solve
 * control, which is the only context this component is mounted in.
 *
 * The capability check itself never touches worker/solve resources (see
 * `liveSolveCapability.ts`'s doc comment); this component adds nothing
 * beyond a single `useEffect` invocation of it, so a failed or skipped
 * check is exactly as cheap as a successful one.
 */
export function LiveSolveAvailabilityNotice() {
  const [result, setResult] = useState<LiveSolveCapabilityResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    checkLiveSolveCapability().then((r) => {
      if (!cancelled) setResult(r);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (result === null || result.supported) return null;

  return (
    <p
      className="mb-4 max-w-[44rem] text-[0.8rem] leading-[1.6] text-fog-dim"
      data-testid="live-solve-unavailable-notice"
    >
      Live in-browser solving isn&rsquo;t available on this device — showing geode-fem&rsquo;s
      precomputed results below instead.
    </p>
  );
}
