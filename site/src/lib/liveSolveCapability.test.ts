/**
 * Unit tests for `checkLiveSolveCapability` (issue #891). Runs in the
 * default "node" vitest environment — no DOM needed, only the global
 * `navigator`/`WebAssembly` shapes the module reads, stubbed per test via
 * `vi.stubGlobal`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MIN_DEVICE_MEMORY_GIB, checkLiveSolveCapability } from "./liveSolveCapability";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checkLiveSolveCapability", () => {
  it("is unsupported when WebAssembly is unavailable, regardless of other checks", async () => {
    vi.stubGlobal("WebAssembly", undefined);
    vi.stubGlobal("navigator", { gpu: undefined, deviceMemory: 8 });

    const result = await checkLiveSolveCapability();

    expect(result.supported).toBe(false);
    expect(result.checks.webAssembly).toBe(false);
    expect(result.reasons.some((r) => /WebAssembly/i.test(r))).toBe(true);
  });

  it("is supported when WebAssembly is present and memory is unreported (unknown, not insufficient)", async () => {
    vi.stubGlobal("WebAssembly", {});
    vi.stubGlobal("navigator", { gpu: undefined });

    const result = await checkLiveSolveCapability();

    expect(result.supported).toBe(true);
    expect(result.checks.webAssembly).toBe(true);
    expect(result.checks.sufficientMemory).toBeNull();
    expect(result.reasons).toEqual([]);
  });

  it("is unsupported when reported device memory is below the threshold", async () => {
    vi.stubGlobal("WebAssembly", {});
    vi.stubGlobal("navigator", { gpu: undefined, deviceMemory: MIN_DEVICE_MEMORY_GIB - 1 });

    const result = await checkLiveSolveCapability();

    expect(result.supported).toBe(false);
    expect(result.checks.sufficientMemory).toBe(false);
    expect(result.reasons.some((r) => /memory/i.test(r))).toBe(true);
  });

  it("is supported when reported device memory meets the threshold", async () => {
    vi.stubGlobal("WebAssembly", {});
    vi.stubGlobal("navigator", { gpu: undefined, deviceMemory: MIN_DEVICE_MEMORY_GIB });

    const result = await checkLiveSolveCapability();

    expect(result.supported).toBe(true);
    expect(result.checks.sufficientMemory).toBe(true);
  });

  it("reports webGpu true when navigator.gpu.requestAdapter() resolves a non-null adapter", async () => {
    vi.stubGlobal("WebAssembly", {});
    const requestAdapter = vi.fn().mockResolvedValue({ requestDevice: vi.fn() });
    vi.stubGlobal("navigator", { gpu: { requestAdapter }, deviceMemory: 8 });

    const result = await checkLiveSolveCapability();

    expect(result.checks.webGpu).toBe(true);
    expect(requestAdapter).toHaveBeenCalledTimes(1);
  });

  it("reports webGpu false, but stays supported, when requestAdapter resolves null (WebGPU absent doesn't gate today)", async () => {
    vi.stubGlobal("WebAssembly", {});
    const requestAdapter = vi.fn().mockResolvedValue(null);
    vi.stubGlobal("navigator", { gpu: { requestAdapter }, deviceMemory: 8 });

    const result = await checkLiveSolveCapability();

    expect(result.checks.webGpu).toBe(false);
    // Per docs/design/geode-fem-wasm-webgpu-spike.md §4: WebGPU cannot carry
    // the f64 the solver's oracles need, so its absence is informational
    // only and must not sink the overall verdict.
    expect(result.supported).toBe(true);
  });

  it("reports webGpu false when navigator.gpu is entirely absent", async () => {
    vi.stubGlobal("WebAssembly", {});
    vi.stubGlobal("navigator", { deviceMemory: 8 });

    const result = await checkLiveSolveCapability();

    expect(result.checks.webGpu).toBe(false);
    expect(result.supported).toBe(true);
  });

  it("treats a throwing requestAdapter() as 'not available', not a hard failure", async () => {
    vi.stubGlobal("WebAssembly", {});
    const requestAdapter = vi.fn().mockRejectedValue(new Error("adapter query failed"));
    vi.stubGlobal("navigator", { gpu: { requestAdapter }, deviceMemory: 8 });

    const result = await checkLiveSolveCapability();

    expect(result.checks.webGpu).toBe(false);
    expect(result.supported).toBe(true);
  });

  it("never calls GPUAdapter.requestDevice() — the check must not reserve a real GPU context", async () => {
    vi.stubGlobal("WebAssembly", {});
    const requestDevice = vi.fn();
    const requestAdapter = vi.fn().mockResolvedValue({ requestDevice });
    vi.stubGlobal("navigator", { gpu: { requestAdapter }, deviceMemory: 8 });

    await checkLiveSolveCapability();

    expect(requestDevice).not.toHaveBeenCalled();
  });

  it("does not throw when navigator is entirely undefined (SSR/prerender safety)", async () => {
    vi.stubGlobal("WebAssembly", {});
    vi.stubGlobal("navigator", undefined);

    const result = await checkLiveSolveCapability();

    expect(result.checks.webGpu).toBe(false);
    expect(result.checks.sufficientMemory).toBeNull();
    expect(result.supported).toBe(true);
  });
});
