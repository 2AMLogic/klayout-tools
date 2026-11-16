/**
 * Shared vitest mocks for the WebGL-backed gallery component suites:
 * `makeFakeGL()` builds a fake WebGL context and `installFakeGLContext()`
 * wires it into `HTMLCanvasElement.getContext()` (issue #2337, which
 * consolidated these copy-pasted helpers into one shared home).
 *
 * The sole consumer is the `components/field` suite; the former
 * `components/em/` suites that shared them were removed in issue #4641.
 *
 * This module is test-only: nothing in the app bundle imports it.
 */
import { vi } from "vitest";

/**
 * A minimal fake WebGL context: known parameter/status getters return
 * truthy, object-creating calls return a plain object, everything else is
 * a no-op. Enough for `FieldViewer`'s init + draw effects to run their
 * full real code path (shader "compile", buffer upload, `drawElements`)
 * without a real GPU — this is what a headless CI browser without WebGL
 * would otherwise force through the fallback branch only.
 */
function makeFakeGL(): WebGL2RenderingContext {
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
