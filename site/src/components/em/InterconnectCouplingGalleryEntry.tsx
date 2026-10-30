import { useState } from "react";
import { InterconnectCouplingResult } from "./InterconnectCouplingResult";
import { ProvenancePanel } from "./ProvenancePanel";
import type { EmProvenance } from "./types";

/**
 * The Electromagnetics gallery's *real fleet geometry* entry (issue #842):
 * the coupling capacitance of a real gallery block's own `met1` VGND/VPWR
 * power rails, extracted by geode-fem's real electrostatic FEM solver —
 * closing the loop between klayout-tools.org and the canary program (a
 * visitor sees a block that came out of this repo's own layout flow, and
 * its real extracted parasitics), alongside `PatchAntennaGalleryEntry`'s
 * validated textbook benchmark.
 *
 * Names the connection to the field-solver backend epics this issue's own
 * acceptance criteria require: the MoM epic (#701, shipped as `klt mom`),
 * the PEX epic (#709, shipped as `klt pex`) and the FEM epic (#708, still
 * open). Copy updated in issue #4597 once #701 and #709 had closed.
 */
export function InterconnectCouplingGalleryEntry() {
  const [provenance, setProvenance] = useState<EmProvenance | null>(null);

  return (
    <article
      className="flex flex-col gap-4 rounded-lg border border-border bg-panel px-5 py-4"
      data-testid="em-benchmark-entry-interconnect-coupling"
    >
      <header>
        <h3 className="font-mono text-[1.05rem] text-orange">
          On-chip power/ground coupling — a real gallery block
        </h3>
        <p className="mt-1 text-[0.85rem] leading-[1.6] text-fog-dim">
          The real VGND/VPWR <code>met1</code> power-rail pair of a gallery block, extracted
          straight from its committed GDS — not a hand-drawn shape. A DC multi-conductor
          electrostatic FEM solve (geode-fem) computes the self- and mutual/coupling
          capacitance between the two rails, cross-checked against the sky130 open PDK&rsquo;s
          own published parasitic-capacitance model. <code>klt mom</code> now computes this
          kind of capacitance matrix from a layout and a stackup (
          <a href="https://github.com/2AMLogic/klayout-tools/issues/701">#701</a>), and{" "}
          <code>klt pex</code> feeds extracted parasitics back into post-layout simulation (
          <a href="https://github.com/2AMLogic/klayout-tools/issues/709">#709</a>). A
          full-field FEM backend for <code>klt</code> is still in progress (
          <a href="https://github.com/2AMLogic/klayout-tools/issues/708">#708</a>).
        </p>
      </header>

      <InterconnectCouplingResult onLoad={(data) => setProvenance(data.provenance)} />

      {provenance && <ProvenancePanel provenance={provenance} />}
    </article>
  );
}
