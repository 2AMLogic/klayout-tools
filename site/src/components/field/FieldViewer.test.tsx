// @vitest-environment jsdom
/**
 * Interaction/render tests for `FieldViewer` (issue #841). Covers the
 * graceful no-WebGL fallback (the default in jsdom, which has no real
 * WebGL implementation), the WebGL-available render path (via a minimal
 * fake `WebGLRenderingContext`), the parameter/frequency-index prop +
 * callback, and mesh/field edge cases (empty mesh, scalar-only vs.
 * vector-only data, out-of-range index clamping) — the test plan Curator
 * specified on this issue.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FieldViewer } from "./FieldViewer";
import type { FieldViewerData } from "./types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const quadMesh: FieldViewerData["mesh"] = {
  vertices: [
    [-1, -1, 0],
    [1, -1, 0],
    [1, 1, 0],
    [-1, 1, 0],
  ],
  cells: [[0, 1, 2, 3]],
};

function scalarData(): FieldViewerData {
  return {
    mesh: quadMesh,
    frames: [
      { label: "2.4 GHz", scalar: [0, 0.5, 1, 0.75] },
      { label: "5.0 GHz", scalar: [1, 0.25, 0, 0.4] },
    ],
  };
}

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

function mockWebglAvailable() {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ((..._args: unknown[]) => makeFakeGL()) as any,
  );
}

/**
 * jsdom's real `HTMLCanvasElement.getContext()` logs a noisy "Not
 * implemented" warning to the console before returning `null` — explicitly
 * mock it to return `null` for the fallback-path tests instead, so the
 * WebGL-unavailable case is exercised without that log spam.
 */
function mockWebglUnavailable() {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
}

describe("FieldViewer — no-WebGL fallback", () => {
  it("renders a fallback message instead of throwing when WebGL is unavailable", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={scalarData()} />);
    expect(screen.getByTestId("field-viewer-fallback")).toBeInTheDocument();
    expect(screen.queryByTestId("field-viewer-canvas")).not.toBeInTheDocument();
  });

  it("still renders the parameter slider and legend in the fallback state", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={scalarData()} />);
    expect(screen.getByLabelText("Parameter index")).toBeInTheDocument();
    expect(screen.getByTestId("field-viewer-legend")).toBeInTheDocument();
  });
});

describe("FieldViewer — WebGL available", () => {
  it("renders the canvas and legend, not the fallback message", () => {
    mockWebglAvailable();
    render(<FieldViewer data={scalarData()} />);
    expect(screen.getByTestId("field-viewer-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("field-viewer-legend")).toBeInTheDocument();
    expect(screen.queryByTestId("field-viewer-fallback")).not.toBeInTheDocument();
  });

  it("re-renders without throwing when the frame changes via prop update", () => {
    mockWebglAvailable();
    const { rerender } = render(<FieldViewer data={scalarData()} parameterIndex={0} />);
    expect(() => rerender(<FieldViewer data={scalarData()} parameterIndex={1} />)).not.toThrow();
    expect(screen.getByTestId("field-viewer-parameter-label")).toHaveTextContent("5.0 GHz");
  });

  it("orbit drag and wheel zoom do not throw", () => {
    mockWebglAvailable();
    render(<FieldViewer data={scalarData()} />);
    const canvas = screen.getByTestId("field-viewer-canvas");
    expect(() => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 100 });
      fireEvent.pointerMove(canvas, { clientX: 120, clientY: 80 });
      fireEvent.pointerUp(canvas, { clientX: 120, clientY: 80 });
      fireEvent.wheel(canvas, { deltaY: 10 });
    }).not.toThrow();
  });

  it("reset view button does not throw", () => {
    mockWebglAvailable();
    render(<FieldViewer data={scalarData()} />);
    expect(() => fireEvent.click(screen.getByRole("button", { name: "Reset view" }))).not.toThrow();
  });
});

describe("FieldViewer — parameter index prop and callback", () => {
  it("fires onParameterIndexChange when the built-in slider moves", () => {
    mockWebglUnavailable();
    const onChange = vi.fn();
    render(<FieldViewer data={scalarData()} onParameterIndexChange={onChange} />);
    fireEvent.change(screen.getByLabelText("Parameter index"), { target: { value: "1" } });
    expect(onChange).toHaveBeenCalledWith(1);
  });

  it("follows a controlled parameterIndex prop rather than internal state", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={scalarData()} parameterIndex={1} />);
    expect(screen.getByTestId("field-viewer-parameter-label")).toHaveTextContent("5.0 GHz");
  });

  it("clamps an out-of-range parameterIndex instead of throwing", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={scalarData()} parameterIndex={99} />);
    expect(screen.getByTestId("field-viewer-parameter-label")).toHaveTextContent("5.0 GHz");
  });

  it("clamps a negative parameterIndex instead of throwing", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={scalarData()} parameterIndex={-5} />);
    expect(screen.getByTestId("field-viewer-parameter-label")).toHaveTextContent("2.4 GHz");
  });

  it("does not render a slider for a single-frame dataset", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={{ mesh: quadMesh, frames: [{ label: "dc", scalar: [0, 0, 0, 0] }] }} />);
    expect(screen.queryByLabelText("Parameter index")).not.toBeInTheDocument();
  });
});

describe("FieldViewer — mesh/field edge cases", () => {
  it("renders a distinct empty-mesh message for zero vertices", () => {
    // No canvas is ever mounted for an empty mesh, so getContext() is
    // never called — no mock needed here.
    render(<FieldViewer data={{ mesh: { vertices: [], cells: [] }, frames: [] }} />);
    expect(screen.getByTestId("field-viewer-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("field-viewer-fallback")).not.toBeInTheDocument();
  });

  it("handles a single-vertex (degenerate) mesh without throwing", () => {
    mockWebglUnavailable();
    expect(() =>
      render(<FieldViewer data={{ mesh: { vertices: [[0, 0, 0]], cells: [] }, frames: [{ scalar: [1] }] }} />),
    ).not.toThrow();
  });

  it("supports vector-only field data (colormapped by magnitude)", () => {
    mockWebglAvailable();
    const data: FieldViewerData = {
      mesh: quadMesh,
      frames: [
        {
          label: "vector frame",
          vector: [
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 1, 0],
          ],
        },
      ],
    };
    expect(() => render(<FieldViewer data={data} />)).not.toThrow();
    expect(screen.getByTestId("field-viewer-canvas")).toBeInTheDocument();
  });

  it("renders without a legend when there are no frames at all", () => {
    mockWebglUnavailable();
    render(<FieldViewer data={{ mesh: quadMesh, frames: [] }} />);
    expect(screen.queryByTestId("field-viewer-legend")).not.toBeInTheDocument();
  });
});
