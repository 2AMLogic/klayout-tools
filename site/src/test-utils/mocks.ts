/**
 * Shared vitest mocks for the WebGL-backed gallery component suites
 * (issue #2337).
 *
 * `makeFakeGL()` and `mockFetchOk()` were previously copy-pasted verbatim
 * into every `src/components/em/*.test.tsx` file plus
 * `src/components/field/FieldViewer.test.tsx` — seven identical copies of
 * the same two helpers. They live here instead so there is exactly one
 * maintenance point.
 *
 * This module is test-only: nothing in the app bundle imports it, and it
 * is deliberately *not* under `components/em/` (which would make the more
 * primitive `components/field/` suite depend on the EM layer that already
 * imports *from* it).
 */
import { vi } from "vitest";

/**
 * A resolved-JSON `fetch` mock: one `ok: true` response whose `json()`
 * yields `payload`. Generic over the payload type so both the
 * `EmSiteExport` gallery suites and any future consumer can use it.
 */
export function mockFetchOk<T>(payload: T) {
  return vi.fn(() =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve(payload),
    } as Response),
  );
}

/**
 * A minimal fake WebGL context: known parameter/status getters return
 * truthy, object-creating calls return a plain object, everything else is
 * a no-op. Enough for `FieldViewer`'s init + draw effects to run their
 * full real code path (shader "compile", buffer upload, `drawElements`)
 * without a real GPU — this is what a headless CI browser without WebGL
 * would otherwise force through the fallback branch only.
 */
export function makeFakeGL(): WebGL2RenderingContext {
  const overrides: Record<string, (...args: unknown[]) => unknown> = {
    createShader: () => ({}),
    createProgram: () => ({}),
    createBuffer: () => ({}),
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    getAttribLocation: () => 0,
    getUniformLocation: () => ({}),
    getExtension: () => ({}),
  };
  const handler: ProxyHandler<Record<string, unknown>> = {
    get(target, prop) {
      if (typeof prop === "string" && prop in overrides) return overrides[prop];
      if (typeof prop === "string" && /^[A-Z][A-Z0-9_]*$/.test(prop)) return 1;
      return target[prop as string] ?? (() => undefined);
    },
  };
  return new Proxy({}, handler) as unknown as WebGL2RenderingContext;
}

/**
 * Installs `makeFakeGL()` as the result of `HTMLCanvasElement.getContext()`
 * for the duration of the current test. This is the wiring every
 * `makeFakeGL`-consuming suite needs verbatim; the `eslint-disable` and
 * `as any` exist solely to satisfy `getContext`'s overloaded signature.
 */
export function installFakeGLContext() {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ((..._args: unknown[]) => makeFakeGL()) as any,
  );
}
