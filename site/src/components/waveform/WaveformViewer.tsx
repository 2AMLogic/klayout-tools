import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";
import type { LayoutSignals, SignalsCorner } from "@/data/types";
import type { WaveformData } from "./types";
import { blockAssetUrl } from "@/lib/blockAssets";
import { Badge } from "@/components/ui/badge";
import {
  formatEngineering,
  fullDomain,
  nearestSample,
  zoomDomain,
  type Domain,
} from "./waveformMath";

/**
 * Interactive waveform viewer for the block detail page's Signals section
 * (Epic #90 Phase 2, issue #100).
 *
 * Consumes `layout.signals` (see `@/data/types.ts`'s `LayoutSignals`) —
 * per-PVT-corner measurements plus an optional `waveform` artifact path,
 * fetched client-side (never during SSR/prerender) from wherever
 * `copy-renders.mjs` staged it, matching the `<img src>` pattern the
 * Renders section already uses for block-relative assets.
 *
 * Supports, per the issue's acceptance criteria:
 *   - zoom (in/out/reset, via the domain-clamping helpers in
 *     `waveformMath.ts`)
 *   - cursor placement (click the plot) + readout (nearest-sample values
 *     per visible trace at the cursor's time)
 *   - per-node visibility toggles (checkboxes, one per distinct signal
 *     name across the loaded corners)
 *   - PVT corner overlay/selection (checkboxes, one per corner; multiple
 *     selected corners overlay on the same axes)
 */
export interface WaveformViewerProps {
  slug: string;
  signals: LayoutSignals;
}

const CORNER_COLORS = ["#3ee0e8", "#ffa028", "#ff6a5c", "#9d7cf2", "#6adf7f", "#f2c94c"];
const NODE_DASH: Array<string | undefined> = [undefined, "6 3", "2 3", "1 4"];

type LoadState = "loading" | "error";

/** The sweep-variable (`variables[0]`) name of a waveform, e.g. `"time"`. */
function sweepName(wf: WaveformData): string {
  return wf.variables[0]?.name ?? "x";
}

/** Non-sweep variable names in a waveform, in declared order. */
function nodeNames(wf: WaveformData): string[] {
  return wf.variables.slice(1).map((v) => v.name);
}

function statusColor(status: SignalsCorner["status"]): string {
  if (status === "pass") return "text-cyan";
  if (status === "fail") return "text-orange";
  return "text-red";
}

export function WaveformViewer({ slug, signals }: WaveformViewerProps) {
  const corners = signals.corners;
  const cornerIndex = useMemo(() => {
    const m = new Map<string, number>();
    corners.forEach((c, i) => m.set(c.corner_id, i));
    return m;
  }, [corners]);

  const defaultCornerId =
    signals.default_corner_id && cornerIndex.has(signals.default_corner_id)
      ? signals.default_corner_id
      : corners[0]?.corner_id;

  const [selectedCorners, setSelectedCorners] = useState<Set<string>>(
    () => new Set(defaultCornerId ? [defaultCornerId] : []),
  );
  const [waveforms, setWaveforms] = useState<Record<string, WaveformData | LoadState>>({});
  const [selectedNodes, setSelectedNodes] = useState<Set<string> | null>(null);
  // `null` means "no explicit zoom yet — track the full bounds", so a
  // freshly-loaded waveform's domain never has to wait on an extra effect
  // round-trip; `resetZoom` restores this state too.
  const [zoomOverride, setZoomOverride] = useState<Domain | null>(null);
  const [cursorX, setCursorX] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // Fetch any selected corner's waveform artifact that isn't cached yet.
  useEffect(() => {
    let cancelled = false;
    for (const cornerId of selectedCorners) {
      const corner = corners[cornerIndex.get(cornerId) ?? -1];
      if (!corner?.waveform || waveforms[cornerId] !== undefined) continue;
      setWaveforms((prev) => ({ ...prev, [cornerId]: "loading" }));
      const url = blockAssetUrl(slug, corner.waveform);
      fetch(url)
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json() as Promise<WaveformData>;
        })
        .then((data) => {
          if (cancelled) return;
          setWaveforms((prev) => ({ ...prev, [cornerId]: data }));
        })
        .catch(() => {
          if (cancelled) return;
          setWaveforms((prev) => ({ ...prev, [cornerId]: "error" }));
        });
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCorners, slug]);

  const loadedWaveforms = useMemo(
    () =>
      Object.values(waveforms).filter(
        (w): w is WaveformData => typeof w === "object" && w !== null,
      ),
    [waveforms],
  );

  const allNodes = useMemo(() => {
    const seen = new Set<string>();
    const ordered: string[] = [];
    for (const wf of loadedWaveforms) {
      for (const name of nodeNames(wf)) {
        if (!seen.has(name)) {
          seen.add(name);
          ordered.push(name);
        }
      }
    }
    return ordered;
  }, [loadedWaveforms]);

  // Default node selection: once the first waveform(s) load, show every node.
  useEffect(() => {
    if (selectedNodes === null && allNodes.length > 0) {
      setSelectedNodes(new Set(allNodes));
    }
  }, [allNodes, selectedNodes]);

  const bounds = useMemo(() => fullDomain(loadedWaveforms), [loadedWaveforms]);
  const domain = zoomOverride ?? bounds;

  const nodesToShow = selectedNodes ?? new Set(allNodes);

  const width = 640;
  const height = 260;
  const padding = { top: 10, right: 10, bottom: 10, left: 10 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  const effectiveDomain = domain ?? bounds ?? [0, 1];

  // Shared y-scale across every visible trace (simple, single-axis viewer).
  const yDomain = useMemo(() => {
    let lo = Infinity;
    let hi = -Infinity;
    for (const cornerId of selectedCorners) {
      const wf = waveforms[cornerId];
      if (typeof wf !== "object" || wf === null) continue;
      wf.variables.slice(1).forEach((variable, i) => {
        if (!nodesToShow.has(variable.name)) return;
        for (const row of wf.points) {
          const v = row[i + 1];
          if (v === undefined) continue;
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      });
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1] as Domain;
    if (lo === hi) return [lo - 1, hi + 1] as Domain;
    const pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad] as Domain;
  }, [selectedCorners, waveforms, nodesToShow]);

  function xScale(x: number): number {
    const [lo, hi] = effectiveDomain;
    const w = hi - lo || 1;
    return padding.left + ((x - lo) / w) * plotW;
  }

  function yScale(v: number): number {
    const [lo, hi] = yDomain;
    const h = hi - lo || 1;
    return padding.top + plotH - ((v - lo) / h) * plotH;
  }

  function pathFor(wf: WaveformData, variableIndex: number): string {
    const [lo, hi] = effectiveDomain;
    let d = "";
    let started = false;
    for (const row of wf.points) {
      const x = row[0];
      const v = row[variableIndex];
      if (x === undefined || v === undefined) continue;
      if (x < lo || x > hi) {
        started = false;
        continue;
      }
      const cmd = started ? "L" : "M";
      d += `${cmd}${xScale(x).toFixed(2)},${yScale(v).toFixed(2)} `;
      started = true;
    }
    return d.trim();
  }

  function toggleCorner(id: string) {
    setSelectedCorners((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleNode(name: string) {
    setSelectedNodes((prev) => {
      const base = prev ?? new Set(allNodes);
      const next = new Set(base);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  function zoom(factor: number) {
    if (bounds === null) return;
    setZoomOverride((prev) => zoomDomain(prev ?? bounds, factor, bounds));
  }

  function resetZoom() {
    setZoomOverride(null);
  }

  function handlePlotClick(event: MouseEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0) return;
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const [lo, hi] = effectiveDomain;
    setCursorX(lo + fraction * (hi - lo));
  }

  if (corners.length === 0) {
    return <p className="text-fog-dim">No signals data.</p>;
  }

  const cursorSweepUnit = loadedWaveforms[0] ? sweepName(loadedWaveforms[0]) : "x";

  return (
    <div className="flex flex-col gap-4" data-testid="waveform-viewer">
      <fieldset className="flex flex-wrap gap-2 border-0 p-0" aria-label="PVT corner selection">
        {corners.map((corner, i) => (
          <label
            key={corner.corner_id}
            className="flex cursor-pointer items-center gap-1.5 rounded-full border border-border bg-panel px-2.5 py-1 text-[0.78rem]"
          >
            <input
              type="checkbox"
              checked={selectedCorners.has(corner.corner_id)}
              onChange={() => toggleCorner(corner.corner_id)}
              aria-label={`Toggle corner ${corner.corner_id}`}
            />
            <span
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ background: CORNER_COLORS[i % CORNER_COLORS.length] }}
            />
            <span className="font-mono text-fog">{corner.corner_id}</span>
            <span className={statusColor(corner.status)}>{corner.status}</span>
          </label>
        ))}
      </fieldset>

      {allNodes.length > 0 && (
        <fieldset className="flex flex-wrap gap-2 border-0 p-0" aria-label="Signal visibility">
          {allNodes.map((name, i) => (
            <label
              key={name}
              className="flex cursor-pointer items-center gap-1.5 rounded-full border border-border bg-panel px-2.5 py-1 text-[0.78rem]"
            >
              <input
                type="checkbox"
                checked={nodesToShow.has(name)}
                onChange={() => toggleNode(name)}
                aria-label={`Toggle signal ${name}`}
              />
              <svg width="16" height="8" aria-hidden="true">
                <line
                  x1="0"
                  y1="4"
                  x2="16"
                  y2="4"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeDasharray={NODE_DASH[i % NODE_DASH.length]}
                />
              </svg>
              <span className="font-mono text-fog">{name}</span>
            </label>
          ))}
        </fieldset>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => zoom(0.5)}
          disabled={bounds === null}
          className="rounded-lg border border-border bg-panel px-2.5 py-1 text-[0.8rem] text-fog hover:border-cyan disabled:opacity-40"
        >
          Zoom in
        </button>
        <button
          type="button"
          onClick={() => zoom(2)}
          disabled={bounds === null}
          className="rounded-lg border border-border bg-panel px-2.5 py-1 text-[0.8rem] text-fog hover:border-cyan disabled:opacity-40"
        >
          Zoom out
        </button>
        <button
          type="button"
          onClick={resetZoom}
          disabled={bounds === null}
          className="rounded-lg border border-border bg-panel px-2.5 py-1 text-[0.8rem] text-fog hover:border-cyan disabled:opacity-40"
        >
          Reset zoom
        </button>
        {domain && bounds && (
          <span className="font-mono text-[0.75rem] text-fog-dim">
            {formatEngineering(domain[0], cursorSweepUnit === "time" ? "s" : "")} –{" "}
            {formatEngineering(domain[1], cursorSweepUnit === "time" ? "s" : "")}
          </span>
        )}
      </div>

      {bounds === null ? (
        <p className="text-fog-dim">
          {selectedCorners.size === 0
            ? "Select a corner to load its waveform."
            : "Loading waveform…"}
        </p>
      ) : (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          aria-label="Waveform plot"
          data-testid="waveform-plot"
          className="w-full cursor-crosshair rounded-lg border border-border bg-night"
          onClick={handlePlotClick}
        >
          {Array.from(selectedCorners).map((cornerId) => {
            const wf = waveforms[cornerId];
            if (typeof wf !== "object" || wf === null) return null;
            const color = CORNER_COLORS[(cornerIndex.get(cornerId) ?? 0) % CORNER_COLORS.length];
            return wf.variables.slice(1).map((variable, i) => {
              if (!nodesToShow.has(variable.name)) return null;
              return (
                <path
                  key={`${cornerId}:${variable.name}`}
                  d={pathFor(wf, i + 1)}
                  fill="none"
                  stroke={color}
                  strokeWidth="1.5"
                  strokeDasharray={NODE_DASH[i % NODE_DASH.length]}
                />
              );
            });
          })}
          {cursorX !== null && (
            <line
              x1={xScale(cursorX)}
              x2={xScale(cursorX)}
              y1={padding.top}
              y2={padding.top + plotH}
              stroke="var(--color-fog-dim)"
              strokeWidth="1"
              strokeDasharray="3 3"
            />
          )}
        </svg>
      )}

      {cursorX !== null && (
        <div className="overflow-x-auto rounded-lg border border-border" data-testid="cursor-readout">
          <table className="w-full border-collapse text-sm">
            <tbody>
              <tr className="border-b border-border">
                <th scope="row" className="w-2/5 bg-panel px-3.5 py-2 text-left font-medium text-fog-dim">
                  Cursor {cursorSweepUnit}
                </th>
                <td className="px-3.5 py-2 text-left font-mono text-fog">
                  {formatEngineering(cursorX, cursorSweepUnit === "time" ? "s" : "")}
                </td>
              </tr>
              {Array.from(selectedCorners).flatMap((cornerId) => {
                const wf = waveforms[cornerId];
                if (typeof wf !== "object" || wf === null) return [];
                const sample = nearestSample(wf, cursorX);
                if (!sample) return [];
                return wf.variables.slice(1).map((variable, i) => {
                  if (!nodesToShow.has(variable.name)) return null;
                  const value = sample[i + 1];
                  return (
                    <tr key={`${cornerId}:${variable.name}`} className="border-b border-border last:border-b-0">
                      <th scope="row" className="w-2/5 bg-panel px-3.5 py-2 text-left font-medium text-fog-dim">
                        {cornerId} · {variable.name}
                      </th>
                      <td className="px-3.5 py-2 text-left font-mono text-fog">
                        {value === undefined ? "—" : formatEngineering(value, variable.type === "voltage" ? "V" : "")}
                      </td>
                    </tr>
                  );
                });
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-wrap gap-4">
        {corners
          .filter((c) => selectedCorners.has(c.corner_id) && (c.measurements?.length ?? 0) > 0)
          .map((corner) => (
            <div key={corner.corner_id} className="flex flex-col gap-1.5">
              <p className="font-mono text-[0.78rem] text-fog-dim">{corner.corner_id}</p>
              <ul className="flex flex-wrap gap-1.5">
                {corner.measurements?.map((m) => (
                  <li key={m.name}>
                    <Badge variant={m.status === "pass" ? "default" : "warn"} className="gap-1.5">
                      <span className="text-[0.64rem] tracking-wide text-fog-dim uppercase">{m.name}</span>
                      <span className="font-semibold text-fog">
                        {m.value === null ? "—" : formatEngineering(m.value, m.unit)}
                      </span>
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          ))}
      </div>

      {Object.entries(waveforms).some(([, v]) => v === "error") && (
        <p className="text-red text-[0.8rem]">
          Some corner waveforms failed to load; readouts may be incomplete.
        </p>
      )}
    </div>
  );
}
