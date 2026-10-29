// @vitest-environment jsdom
/**
 * Landing-page content tests (issue #4597): the release badge, verb list and
 * deck list come from the generated `cliData`, not hand-written copy, and the
 * quick start's sample GDS is one this site actually serves.
 */
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { IndexPage } from "./IndexPage";
import { cliData } from "@/data/cliData";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  // The Solver Validation entries fetch their exports; keep them pending.
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  return render(<IndexPage layouts={[]} />);
}

describe("IndexPage", () => {
  it("shows the latest release from the generated CLI data", () => {
    renderPage();
    expect(screen.getByTestId("release-badge")).toHaveTextContent(
      `v${cliData.release.version} on PyPI`,
    );
  });

  it("lists every generated verb exactly once", () => {
    renderPage();
    const groups = screen.getByTestId("capability-groups");
    const all = cliData.groups.flatMap((g) => g.verbs.map((v) => v.name));
    expect(all.length).toBe(cliData.verb_count);
    for (const name of all) {
      const hits = Array.from(groups.querySelectorAll("a, code")).filter(
        (el) => el.textContent === `klt ${name}`,
      );
      expect(hits, name).toHaveLength(1);
    }
  });

  it("names every built-in deck", () => {
    renderPage();
    for (const deck of cliData.decks) {
      expect(screen.getAllByText(deck).length).toBeGreaterThan(0);
    }
  });

  it("no longer presents the CLI as unbuilt scaffolding", () => {
    renderPage();
    expect(screen.queryByText(/scaffold/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/building toward/i)).not.toBeInTheDocument();
  });

  it("points the quick start at a GDS the gallery build publishes", () => {
    renderPage();
    const pre = screen.getByLabelText("first session");
    const match = pre.textContent?.match(/klayout-tools\.org\/blocks\/([^/]+)\/([^\s]+\.gds)/);
    expect(match).not.toBeNull();
    const [, slug, file] = match!;
    // copy-renders.mjs serves blocks/<slug>/output/<file> at /blocks/<slug>/<file>.
    expect(existsSync(resolve(REPO_ROOT, "blocks", slug, "output", file))).toBe(true);
  });
});
