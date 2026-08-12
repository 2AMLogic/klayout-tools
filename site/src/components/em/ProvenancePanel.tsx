import { Table, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import type { EmProvenance } from "./types";

/**
 * Reproducibility panel for one geode-fem EM gallery entry (Epic #840 Phase
 * 1c, issue #851): renders `EmSiteExport.provenance` — solver version +
 * commit, geometry, and solve parameters — alongside the field/S-parameter
 * result it describes, so every visual in the gallery is auditable rather
 * than a bare "trust me" (the schema's own framing,
 * `docs/schemas/em-site-export.schema.json`'s `provenance` description).
 *
 * Generic across benchmarks: `solve_parameters` varies by benchmark family
 * (the schema itself says so), so it is rendered as a plain key/value list
 * rather than typed field-by-field — this panel needs no changes when a
 * second benchmark (e.g. spiral inductor, Mie sphere) ships its own export
 * with a different `solve_parameters` shape.
 */
export interface ProvenancePanelProps {
  provenance: EmProvenance;
}

/** `snake_case` -> `Title Case`, for rendering `solve_parameters` keys. */
function humanizeKey(key: string): string {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function ProvenancePanel({ provenance }: ProvenancePanelProps) {
  const { generator, geometry, solve_parameters, generated_at } = provenance;
  const commitShort = generator.commit.slice(0, 12);
  const repoLabel = generator.repo.replace(/^https?:\/\/(www\.)?/, "");
  const solveParamEntries = Object.entries(solve_parameters ?? {});

  return (
    <div
      data-testid="em-provenance-panel"
      className="rounded-lg border border-border bg-panel px-4 py-3.5"
    >
      <h4 className="mb-2.5 font-mono text-[0.78rem] tracking-[0.08em] text-cyan uppercase">
        Provenance
      </h4>
      <Table>
        <TableBody>
          <TableRow>
            <TableHead>Solver</TableHead>
            <TableCell>
              <a href={generator.repo}>{repoLabel}</a>
              {generator.version && ` v${generator.version}`}
              {" @ "}
              <span data-testid="em-provenance-commit">{commitShort}</span>
              {generator.build_profile && (
                <span className="text-fog-dim"> · {generator.build_profile}</span>
              )}
            </TableCell>
          </TableRow>
          {generator.backend && (
            <TableRow>
              <TableHead>Backend</TableHead>
              <TableCell>{generator.backend}</TableCell>
            </TableRow>
          )}
          <TableRow>
            <TableHead>Geometry</TableHead>
            <TableCell>
              {geometry.description ?? geometry.fixture}
              {geometry.description && (
                <span className="mt-0.5 block text-[0.78rem] text-fog-dim">
                  {geometry.fixture}
                  {geometry.fixture_sha256 && ` · sha256 ${geometry.fixture_sha256.slice(0, 12)}…`}
                </span>
              )}
            </TableCell>
          </TableRow>
          {geometry.source_repo && geometry.source_commit && (
            <TableRow>
              <TableHead>Source</TableHead>
              <TableCell>
                <a href={geometry.source_repo}>
                  {geometry.source_repo.replace(/^https?:\/\/(www\.)?/, "")}
                </a>
                {" @ "}
                <span data-testid="em-provenance-source-commit">{geometry.source_commit.slice(0, 12)}</span>
                {geometry.source_block && (
                  <span className="text-fog-dim"> · block {geometry.source_block}</span>
                )}
              </TableCell>
            </TableRow>
          )}
          {solveParamEntries.length > 0 && (
            <TableRow>
              <TableHead>Solve parameters</TableHead>
              <TableCell>
                <ul className="flex flex-col gap-0.5">
                  {solveParamEntries.map(([key, value]) => (
                    <li key={key}>
                      <span className="text-fog-dim">{humanizeKey(key)}</span>:{" "}
                      {value === null ? "—" : String(value)}
                    </li>
                  ))}
                </ul>
              </TableCell>
            </TableRow>
          )}
          <TableRow>
            <TableHead>Generated</TableHead>
            <TableCell>{generated_at}</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  );
}
