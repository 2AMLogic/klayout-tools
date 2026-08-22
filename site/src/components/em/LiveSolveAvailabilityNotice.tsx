import { useEffect, useState } from "react";
import { checkLiveSolveCapability, type LiveSolveCapabilityResult } from "@/lib/liveSolveCapability";

/**
 * Graceful-fallback wiring for the Electromagnetics gallery (Epic #840
 * Phase 2c, issue #891): runs {@link checkLiveSolveCapability} once on
 * mount and, only when the browser can't support an in-browser solve,
 * renders a small, non-alarming inline note above the gallery entries.
 *
 * **There is no live-solve UI in this repo yet** (Phase 2a/2b/2d — issues
 * #889/#890/#892 — are unbuilt; see
 * `docs/design/geode-fem-wasm-webgpu-spike.md`'s DEFER verdict). So today
 * `EmGallerySection`'s benchmark entries (`PatchAntennaGalleryEntry` etc.)
 * always render Phase 1's pre-computed results regardless of what this
 * component finds — the "fallback" the acceptance criteria asks for is
 * already the section's only rendering path, and stays that way whether
 * this component's check passes or fails. This component's job is narrower
 * and forward-looking: surface the capability signal now, and give a
 * future live-solve interaction (whenever #890/#892 land) a component to
 * wrap around instead of duplicating the check-and-notice logic.
 *
 * When the check finds the browser capable, this renders nothing — there
 * is no live-solve feature yet to announce as "available", and a
 * self-congratulatory "you *could* run this" note with nothing behind it
 * would be misleading.
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
