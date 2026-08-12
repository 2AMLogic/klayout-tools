// @vitest-environment jsdom
/**
 * Render tests for `ProvenancePanel` (issue #851) against a
 * `provenance`-shaped fixture mirroring the committed
 * `examples/em/patch_antenna/patch_antenna.em-export.json`.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ProvenancePanel } from "./ProvenancePanel";
import type { EmProvenance } from "./types";

const provenance: EmProvenance = {
  generator: {
    repo: "https://github.com/rjwalters/geode-fem",
    license: "MIT",
    commit: "90759f103fdbdc42e47b1941ccd8d0e0b031c4e6",
    version: "0.3.0",
    backend: "burn::backend::NdArray<f64, i32> (CPU)",
    build_profile: "release",
  },
  geometry: {
    fixture: "tests/fixtures/patch_2g4.msh",
    fixture_sha256: "01c55cbd359b97d2fdceae9630a86d6fa2342f9a18317c01d61ad41a1df05f8",
    description: "Probe-fed FR-4 rectangular microstrip patch.",
  },
  solve_parameters: { port_resistance_ohm: 50, substrate: "fr4", eps_r: 4.4 },
  generated_at: "2026-08-12T04:15:40Z",
};

afterEach(() => {
  cleanup();
});

describe("ProvenancePanel", () => {
  it("renders the solver repo, version, and truncated commit", () => {
    render(<ProvenancePanel provenance={provenance} />);
    expect(screen.getByTestId("em-provenance-panel")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "github.com/rjwalters/geode-fem" })).toHaveAttribute(
      "href",
      "https://github.com/rjwalters/geode-fem",
    );
    expect(screen.getByText(/v0\.3\.0/)).toBeInTheDocument();
    expect(screen.getByTestId("em-provenance-commit")).toHaveTextContent("90759f103fdb");
  });

  it("renders the geometry description and fixture path", () => {
    render(<ProvenancePanel provenance={provenance} />);
    expect(screen.getByText(/Probe-fed FR-4 rectangular microstrip patch/)).toBeInTheDocument();
    expect(screen.getByText(/tests\/fixtures\/patch_2g4\.msh/)).toBeInTheDocument();
  });

  it("renders solve_parameters as humanized key/value rows", () => {
    render(<ProvenancePanel provenance={provenance} />);
    expect(screen.getByText("Port Resistance Ohm")).toBeInTheDocument();
    expect(screen.getByText(/: 50/)).toBeInTheDocument();
    expect(screen.getByText("Substrate")).toBeInTheDocument();
    expect(screen.getByText(/: fr4/)).toBeInTheDocument();
  });

  it("omits the solve parameters row entirely when there are none", () => {
    render(<ProvenancePanel provenance={{ ...provenance, solve_parameters: undefined }} />);
    expect(screen.queryByText("Solve parameters")).not.toBeInTheDocument();
  });

  it("renders the generated_at timestamp", () => {
    render(<ProvenancePanel provenance={provenance} />);
    expect(screen.getByText("2026-08-12T04:15:40Z")).toBeInTheDocument();
  });
});
