import { useEffect, useState } from "react";
import { FieldViewer } from "@/components/field/FieldViewer";
import type { FieldViewerData } from "@/components/field/types";
import { Table, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import type { EmSiteExport } from "./types";

/**
 * EM-gallery result view for the `interconnect_coupling` benchmark (issue
 * #842) — the Electromagnetics gallery's *real fleet geometry* entry,
 * alongside `PatchAntennaResult`'s textbook benchmark. Unlike that
 * component, there is no frequency sweep to drive a slider off: this
 * export is a single DC electrostatic solve (VPWR driven to 1V, VGND at
 * 0V), so `FieldViewer` renders the lone frame directly and the rest of
 * this component renders the `capacitance` block's Maxwell matrix +
 * honest oracle cross-check as a plain table — the "extracted L/C/coupling
 * is the point" framing from this issue's own scope.
 *
 * No illustrative/faked fields: every value rendered here traces directly
 * to the fetched, committed export (`examples/em/interconnect_coupling/generate.py`'s
 * output) — see that script's own docstring for how it derives the real
 * GDS geometry and drives geode-fem's real electrostatic FEM solver.
 */
export interface InterconnectCouplingResultProps {
  /** Where the staged export JSON lives (`site/scripts/stage-em-data.mjs`
   *  stages `examples/em/interconnect_coupling/interconnect_coupling.em-export.json`
   *  here at build time). Overridable for tests. */
  dataUrl?: string;
  /** Fired once the export fetch resolves successfully, with the full
   *  parsed document (mirrors `PatchAntennaResult`'s `onLoad`: lets a
   *  gallery-entry wrapper render a `ProvenancePanel` from the same
   *  fetched document). Never called on a failed/loading fetch. */
  onLoad?: (data: EmSiteExport) => void;
}

type LoadState = "loading" | "error" | EmSiteExport;

/** SI-prefix labels for powers of 1000 from 10^-3 (milli) down to 10^-18
 *  (atto) -- on-chip capacitances land in the femto/atto range. */
const SI_SUB_UNITS: Record<number, string> = {
  [-3]: "mF",
  [-6]: "uF",
  [-9]: "nF",
  [-12]: "pF",
  [-15]: "fF",
  [-18]: "aF",
};

function formatFarad(value: number): string {
  const abs = Math.abs(value);
  if (abs === 0) return "0 F";
  // Standard engineering-notation bucketing: the exponent (a multiple of
  // 3) such that `abs / 10^exponent` lands in `[1, 1000)`.
  let exponent = Math.floor(Math.log10(abs) / 3) * 3;
  exponent = Math.min(-3, Math.max(-18, exponent));
  const label = SI_SUB_UNITS[exponent] ?? `e${exponent} F`;
  return `${(value / 10 ** exponent).toFixed(3)} ${label}`;
}

export function InterconnectCouplingResult({
  dataUrl = "/em/interconnect_coupling/interconnect_coupling.em-export.json",
  onLoad,
}: InterconnectCouplingResultProps) {
  const [state, setState] = useState<LoadState>("loading");

  // Client-side fetch only (never during SSR/prerender), matching
  // `PatchAntennaResult`'s own client-fetch convention.
  useEffect(() => {
    let cancelled = false;
    setState("loading");
    fetch(dataUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<EmSiteExport>;
      })
      .then((data) => {
        if (cancelled) return;
        setState(data);
        onLoad?.(data);
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onLoad is a
    // caller-supplied callback, not a re-fetch trigger; see
    // PatchAntennaResult's identical guard for the full rationale.
  }, [dataUrl]);

  if (state === "loading") {
    return (
      <p className="text-fog-dim" data-testid="interconnect-coupling-loading">
        Loading interconnect-coupling solve…
      </p>
    );
  }

  const data = typeof state === "object" ? state : null;
  const capacitance = data?.capacitance;
  if (state === "error" || !data || !capacitance) {
    return (
      <p className="text-red" data-testid="interconnect-coupling-error">
        Failed to load the interconnect-coupling benchmark's exported solver data.
      </p>
    );
  }

  const fieldData: FieldViewerData = { mesh: data.mesh, frames: data.frames };
  const { conductors, matrix_farad: matrix, oracle } = capacitance;

  return (
    <div className="flex flex-col gap-6" data-testid="interconnect-coupling-result">
      <div className="flex flex-col gap-2">
        <FieldViewer data={fieldData} />
        <p className="text-[0.8rem] text-fog-dim" data-testid="interconnect-coupling-frame-note">
          {data.frames[0]?.label ?? "Driven field"} — potential φ (colormap) and E field around
          the two real power-rail conductors, mid-length cross-section.
        </p>
      </div>

      <div>
        <h4 className="mb-2 font-mono text-[0.78rem] tracking-[0.08em] text-cyan uppercase">
          Extracted capacitance
        </h4>
        <Table>
          <TableBody>
            {conductors.map((name, i) => (
              <TableRow key={name}>
                <TableHead>{`C(${name}, ${name})`}</TableHead>
                <TableCell data-testid={`interconnect-coupling-self-cap-${name}`}>
                  {formatFarad(matrix[i][i])}
                </TableCell>
              </TableRow>
            ))}
            {conductors.length > 1 && (
              <TableRow>
                <TableHead>{`C(${conductors[0]}, ${conductors[1]}) — mutual`}</TableHead>
                <TableCell data-testid="interconnect-coupling-mutual-cap">
                  {formatFarad(matrix[0][1])}
                </TableCell>
              </TableRow>
            )}
            {oracle && (
              <TableRow>
                <TableHead>Oracle cross-check</TableHead>
                <TableCell data-testid="interconnect-coupling-oracle">
                  {formatFarad(oracle.predicted_self_capacitance_farad)} predicted vs.{" "}
                  {formatFarad(oracle.fem_self_capacitance_farad)} FEM
                  {oracle.rel_err !== null && ` (${(oracle.rel_err * 100).toFixed(1)}% rel. err.)`}
                  {oracle.source && (
                    <span className="mt-0.5 block text-[0.78rem] text-fog-dim">{oracle.source}</span>
                  )}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
