import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import type { FieldFrame, FieldViewerData } from "./types";
import {
  clamp,
  frameColors,
  frameDomain,
  formatMagnitude,
  legendGradientCss,
  lookAt,
  meshBounds,
  multiplyMat4,
  orbitEye,
  perspective,
  triangulateCells,
} from "./fieldMath";

/**
 * Reusable 3D mesh + scalar/vector field viewer (issue #841) — the shared
 * viz primitive the electromagnetics gallery (geode-fem browser-EM epic,
 * #840) needs for its S-parameter/field-solve results, and that a future 3D
 * layout viewer can reuse. Driven entirely by data props (`data: {mesh,
 * frames}`, see `types.ts`) — no solver dependency, no fetch, no server.
 *
 * Rendering is raw WebGL (WebGL2 preferred, WebGL1 fallback), not
 * three.js/`@react-three/fiber`: as of this issue, `site/package.json` has
 * no 3D library dependency at all (confirmed by Curator review), and a
 * single unlit, per-vertex-colored triangle mesh is well within what a
 * ~150-line raw-WebGL renderer can do correctly — so this avoids adding a
 * ~600KB+ dependency to a static site's bundle for a feature that doesn't
 * need it. If a future issue needs lighting/materials/scene-graph
 * complexity beyond a colormapped mesh, revisit that tradeoff then.
 *
 * Static-deploy friendly like `WaveformViewer`: all WebGL setup happens in
 * `useEffect` (never during `renderToString`/SSR), so this component is
 * safe to use on a prerendered page. When WebGL is unavailable (or shader
 * setup fails, e.g. under a restrictive headless browser), it degrades to a
 * message instead of throwing.
 */
export interface FieldViewerProps {
  data: FieldViewerData;
  /** Controls which `data.frames[]` entry is displayed. Omit to let the
   *  component manage its own selection (still scrubbable via the built-in
   *  slider); pass it to drive `FieldViewer` from an external control
   *  (e.g. `WaveformViewer`'s own frequency axis, per this issue's scope). */
  parameterIndex?: number;
  /** Fired whenever the displayed frame changes — from the built-in
   *  slider, or simply to notify a controlling parent of the effective
   *  clamped index. */
  onParameterIndexChange?: (index: number) => void;
  /** Canvas pixel size. Defaults match `WaveformViewer`'s plot area scale. */
  width?: number;
  height?: number;
}

interface OrbitState {
  yaw: number;
  pitch: number;
  zoom: number;
}

const DEFAULT_ORBIT: OrbitState = { yaw: -Math.PI / 4, pitch: Math.PI / 6, zoom: 1 };

const VERTEX_SHADER = `
attribute vec3 aPosition;
attribute vec3 aColor;
uniform mat4 uModelViewProjection;
varying vec3 vColor;
void main() {
  gl_Position = uModelViewProjection * vec4(aPosition, 1.0);
  vColor = aColor;
}
`;

const FRAGMENT_SHADER = `
precision mediump float;
varying vec3 vColor;
void main() {
  gl_FragColor = vec4(vColor, 1.0);
}
`;

type GL = WebGLRenderingContext | WebGL2RenderingContext;

function compileShader(gl: GL, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("Failed to create shader");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`Shader compile error: ${info ?? "unknown"}`);
  }
  return shader;
}

function createProgram(gl: GL, vsSource: string, fsSource: string): WebGLProgram {
  const vertexShader = compileShader(gl, gl.VERTEX_SHADER, vsSource);
  const fragmentShader = compileShader(gl, gl.FRAGMENT_SHADER, fsSource);
  const program = gl.createProgram();
  if (!program) throw new Error("Failed to create program");
  gl.attachShader(program, vertexShader);
  gl.attachShader(program, fragmentShader);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(`Program link error: ${info ?? "unknown"}`);
  }
  gl.deleteShader(vertexShader);
  gl.deleteShader(fragmentShader);
  return program;
}

interface GLState {
  gl: GL;
  program: WebGLProgram;
  positionBuffer: WebGLBuffer;
  colorBuffer: WebGLBuffer;
  indexBuffer: WebGLBuffer;
  aPosition: number;
  aColor: number;
  uMvp: WebGLUniformLocation | null;
}

export function FieldViewer({ data, parameterIndex, onParameterIndexChange, width = 640, height = 420 }: FieldViewerProps) {
  const { mesh, frames } = data;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const glStateRef = useRef<GLState | null>(null);
  const dragRef = useRef<{ x: number; y: number } | null>(null);

  const [webglSupported, setWebglSupported] = useState<boolean | null>(null);
  const [internalIndex, setInternalIndex] = useState(0);
  const [orbit, setOrbit] = useState<OrbitState>(DEFAULT_ORBIT);

  const maxIndex = Math.max(0, frames.length - 1);
  const isControlled = parameterIndex !== undefined;
  const activeIndex = clamp(isControlled ? (parameterIndex as number) : internalIndex, 0, maxIndex);
  const frame: FieldFrame | undefined = frames[activeIndex];

  const bounds = useMemo(() => meshBounds(mesh.vertices), [mesh.vertices]);
  const triangleIndices = useMemo(() => triangulateCells(mesh.cells), [mesh.cells]);

  function handleIndexChange(next: number) {
    const clamped = clamp(next, 0, maxIndex);
    if (!isControlled) setInternalIndex(clamped);
    onParameterIndexChange?.(clamped);
  }

  // One-time WebGL setup: acquire a context, compile the (single, static)
  // shader program, allocate the three GPU buffers this component ever
  // needs. Never re-runs on prop changes — recompiling shaders per
  // orbit-drag frame would be wasteful; only the draw effect below re-runs.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl =
      (canvas.getContext("webgl2") as WebGL2RenderingContext | null) ??
      (canvas.getContext("webgl") as WebGLRenderingContext | null);
    if (!gl) {
      setWebglSupported(false);
      return;
    }

    let program: WebGLProgram;
    try {
      program = createProgram(gl, VERTEX_SHADER, FRAGMENT_SHADER);
    } catch (err) {
      console.error("FieldViewer: WebGL shader setup failed", err);
      setWebglSupported(false);
      return;
    }

    const positionBuffer = gl.createBuffer();
    const colorBuffer = gl.createBuffer();
    const indexBuffer = gl.createBuffer();
    if (!positionBuffer || !colorBuffer || !indexBuffer) {
      gl.deleteProgram(program);
      setWebglSupported(false);
      return;
    }

    glStateRef.current = {
      gl,
      program,
      positionBuffer,
      colorBuffer,
      indexBuffer,
      aPosition: gl.getAttribLocation(program, "aPosition"),
      aColor: gl.getAttribLocation(program, "aColor"),
      uMvp: gl.getUniformLocation(program, "uModelViewProjection"),
    };
    setWebglSupported(true);

    return () => {
      gl.deleteProgram(program);
      gl.deleteBuffer(positionBuffer);
      gl.deleteBuffer(colorBuffer);
      gl.deleteBuffer(indexBuffer);
      glStateRef.current = null;
    };
  }, []);

  // Re-draw whenever the mesh, the displayed frame's field data, or the
  // camera (orbit/zoom) changes. Re-uploads position/color/index data each
  // draw for correctness/simplicity (this component's target mesh sizes
  // are viewer-scale, not a real-time simulation) but never recompiles the
  // shader program.
  useEffect(() => {
    const state = glStateRef.current;
    if (!state || !frame) return;
    const { gl, program, positionBuffer, colorBuffer, indexBuffer, aPosition, aColor, uMvp } = state;

    const vertexCount = mesh.vertices.length;
    const positions = new Float32Array(vertexCount * 3);
    mesh.vertices.forEach((v, i) => {
      positions[i * 3] = v[0] ?? 0;
      positions[i * 3 + 1] = v[1] ?? 0;
      positions[i * 3 + 2] = v[2] ?? 0;
    });
    const colors = frameColors(frame, vertexCount);

    const useUint32 = vertexCount > 65535;
    let indexData: Uint16Array | Uint32Array;
    let indexType: number;
    if (useUint32) {
      const isWebGL2 = typeof WebGL2RenderingContext !== "undefined" && gl instanceof WebGL2RenderingContext;
      const ext = isWebGL2 || gl.getExtension("OES_element_index_uint");
      if (ext) {
        indexData = Uint32Array.from(triangleIndices);
        indexType = gl.UNSIGNED_INT;
      } else {
        // No safe way to index this many vertices — draw nothing rather
        // than corrupt/crash. Extremely unlikely for a viewer-scale mesh.
        indexData = new Uint16Array(0);
        indexType = gl.UNSIGNED_SHORT;
      }
    } else {
      indexData = Uint16Array.from(triangleIndices);
      indexType = gl.UNSIGNED_SHORT;
    }

    gl.viewport(0, 0, width, height);
    gl.clearColor(0.043, 0.055, 0.075, 1); // matches --color-night
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(program);

    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(aPosition);
    gl.vertexAttribPointer(aPosition, 3, gl.FLOAT, false, 0, 0);

    gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, colors, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(aColor);
    gl.vertexAttribPointer(aColor, 3, gl.FLOAT, false, 0, 0);

    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indexData, gl.STATIC_DRAW);

    const eye = orbitEye(bounds.center, bounds.radius * 2.6 * orbit.zoom, orbit.yaw, orbit.pitch);
    const view = lookAt(eye, bounds.center, [0, 1, 0]);
    const projection = perspective(
      (Math.PI / 180) * 45,
      width / (height || 1),
      Math.max(bounds.radius * 0.01, 0.01),
      bounds.radius * 20 + 10,
    );
    const mvp = multiplyMat4(projection, view);
    gl.uniformMatrix4fv(uMvp, false, mvp);

    if (indexData.length > 0) {
      gl.drawElements(gl.TRIANGLES, indexData.length, indexType, 0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mesh, frame, triangleIndices, orbit, bounds, width, height]);

  function handlePointerDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    dragRef.current = { x: event.clientX, y: event.clientY };
    if (typeof event.currentTarget.setPointerCapture === "function") {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    const start = dragRef.current;
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    dragRef.current = { x: event.clientX, y: event.clientY };
    setOrbit((prev) => ({
      ...prev,
      yaw: prev.yaw - dx * 0.01,
      pitch: clamp(prev.pitch - dy * 0.01, -1.5, 1.5),
    }));
  }

  function handlePointerUp(event: ReactPointerEvent<HTMLCanvasElement>) {
    dragRef.current = null;
    if (
      typeof event.currentTarget.hasPointerCapture === "function" &&
      typeof event.currentTarget.releasePointerCapture === "function" &&
      event.currentTarget.hasPointerCapture(event.pointerId)
    ) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function handleWheel(event: ReactWheelEvent<HTMLCanvasElement>) {
    event.preventDefault();
    setOrbit((prev) => ({
      ...prev,
      zoom: clamp(prev.zoom * (event.deltaY > 0 ? 1.1 : 0.9), 0.2, 5),
    }));
  }

  function resetView() {
    setOrbit(DEFAULT_ORBIT);
  }

  const parameterControl = frames.length > 1 && (
    <div className="flex flex-wrap items-center gap-2">
      <label className="text-[0.8rem] text-fog-dim" htmlFor="field-viewer-parameter-index">
        Parameter
      </label>
      <input
        id="field-viewer-parameter-index"
        type="range"
        min={0}
        max={maxIndex}
        step={1}
        value={activeIndex}
        onChange={(event) => handleIndexChange(Number(event.target.value))}
        aria-label="Parameter index"
        className="accent-cyan"
      />
      <span className="font-mono text-[0.8rem] text-fog" data-testid="field-viewer-parameter-label">
        {frame?.label ?? `#${activeIndex}`}
      </span>
    </div>
  );

  const legend = frame && (
    <div className="flex items-center gap-2" data-testid="field-viewer-legend">
      <span className="font-mono text-[0.75rem] text-fog-dim">{formatMagnitude(frameDomain(frame)[0])}</span>
      <span
        className="h-2.5 w-32 rounded-full border border-border"
        style={{ background: legendGradientCss() }}
        aria-hidden="true"
      />
      <span className="font-mono text-[0.75rem] text-fog-dim">{formatMagnitude(frameDomain(frame)[1])}</span>
    </div>
  );

  if (mesh.vertices.length === 0) {
    return (
      <div className="flex flex-col gap-3" data-testid="field-viewer">
        <p className="text-fog-dim" data-testid="field-viewer-empty">
          No mesh data.
        </p>
      </div>
    );
  }

  if (webglSupported === false) {
    return (
      <div className="flex flex-col gap-3" data-testid="field-viewer">
        {parameterControl}
        {legend}
        <p className="text-fog-dim" data-testid="field-viewer-fallback">
          3D preview unavailable — WebGL is not supported in this browser.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="field-viewer">
      <div className="flex flex-wrap items-center gap-3">
        {parameterControl}
        <button
          type="button"
          onClick={resetView}
          className="rounded-lg border border-border bg-panel px-2.5 py-1 text-[0.8rem] text-fog hover:border-cyan"
        >
          Reset view
        </button>
      </div>
      <canvas
        ref={canvasRef}
        width={width}
        height={height}
        data-testid="field-viewer-canvas"
        role="img"
        aria-label="3D field viewer — drag to orbit, scroll to zoom"
        className="w-full cursor-grab touch-none rounded-lg border border-border bg-night active:cursor-grabbing"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerUp}
        onWheel={handleWheel}
      />
      {legend}
    </div>
  );
}
