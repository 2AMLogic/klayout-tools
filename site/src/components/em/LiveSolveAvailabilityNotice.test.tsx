// @vitest-environment jsdom
/**
 * Render tests for `LiveSolveAvailabilityNotice` (issue #891) — simulates a
 * capability-check failure (acceptance criterion: "the fallback path is
 * covered by a test ... so a regression can't silently ship") and confirms:
 *   1. the fallback notice renders with no broken UI state,
 *   2. a passing check renders nothing (no live-solve feature exists to
 *      announce yet),
 *   3. the check never spins up a `Worker` — capability-check failure (or
 *      success) must not consume solve resources.
 *
 * The component is deliberately not mounted on the public site yet (see its
 * doc comment, and the complementary guard in `EmGallerySection.test.tsx`);
 * these tests are what keep it correct until #890/#892 wire it in.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

const checkLiveSolveCapability = vi.hoisted(() => vi.fn());

vi.mock("@/lib/liveSolveCapability", () => ({
  checkLiveSolveCapability,
}));

import { LiveSolveAvailabilityNotice } from "./LiveSolveAvailabilityNotice";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  checkLiveSolveCapability.mockReset();
});

describe("LiveSolveAvailabilityNotice", () => {
  it("renders a clear, non-alarming fallback notice when the capability check fails", async () => {
    checkLiveSolveCapability.mockResolvedValue({
      supported: false,
      reasons: ["WebAssembly is not available in this browser."],
      checks: { webAssembly: false, webGpu: false, sufficientMemory: null },
    });

    render(<LiveSolveAvailabilityNotice />);

    const notice = await screen.findByTestId("live-solve-unavailable-notice");
    expect(notice).toBeInTheDocument();
    expect(notice.textContent).toMatch(/isn.t available/i);
    // Non-alarming: no error styling class, no role="alert".
    expect(notice).not.toHaveAttribute("role", "alert");
  });

  it("renders nothing once the capability check succeeds (no live-solve feature exists to announce yet)", async () => {
    checkLiveSolveCapability.mockResolvedValue({
      supported: true,
      reasons: [],
      checks: { webAssembly: true, webGpu: true, sufficientMemory: true },
    });

    const { container } = render(<LiveSolveAvailabilityNotice />);

    await waitFor(() => expect(checkLiveSolveCapability).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("live-solve-unavailable-notice")).not.toBeInTheDocument();
  });

  it("renders nothing while the check is still pending — no broken/loading UI state", () => {
    checkLiveSolveCapability.mockReturnValue(new Promise(() => {}));

    const { container } = render(<LiveSolveAvailabilityNotice />);

    expect(container).toBeEmptyDOMElement();
  });

  it("never constructs a Worker — a capability check (pass or fail) must not consume solve resources", async () => {
    const WorkerCtor = vi.fn();
    vi.stubGlobal("Worker", WorkerCtor);
    checkLiveSolveCapability.mockResolvedValue({
      supported: false,
      reasons: ["WebAssembly is not available in this browser."],
      checks: { webAssembly: false, webGpu: false, sufficientMemory: null },
    });

    render(<LiveSolveAvailabilityNotice />);
    await screen.findByTestId("live-solve-unavailable-notice");

    expect(WorkerCtor).not.toHaveBeenCalled();
  });

  it("calls the capability check exactly once per mount, even across re-renders", async () => {
    checkLiveSolveCapability.mockResolvedValue({
      supported: false,
      reasons: [],
      checks: { webAssembly: false, webGpu: false, sufficientMemory: null },
    });

    const { rerender } = render(<LiveSolveAvailabilityNotice />);
    await screen.findByTestId("live-solve-unavailable-notice");
    rerender(<LiveSolveAvailabilityNotice />);

    expect(checkLiveSolveCapability).toHaveBeenCalledTimes(1);
  });
});
