import type { ReactNode } from "react";
import type { Layout } from "@/data/types";
import { cliData } from "@/data/cliData";
import { LayoutCard } from "@/components/LayoutCard";

/**
 * klayout-tools.org landing page (ported from `index.astro` in issue #92's
 * Astro -> React migration; originally issue #59/#63; content refresh in
 * issue #4597).
 *
 * Sections, top to bottom: hero + vision statement, Quick start (install,
 * a runnable first session against a GDS this site serves, MCP, CI),
 * Capabilities (every `klt` verb, grouped), Gallery (one card per block
 * discovered at build time by `loadLayouts()`, issue #63, each linking to
 * its `/<slug>/` detail page, issue #64) and Docs.
 *
 * Everything that drifts with the code — the release version, the verb
 * list and summaries, the MCP tool count and the built-in deck names —
 * comes from `cliData` (`src/data/cli.generated.json`), which
 * `scripts/gen_site_cli_data.py` generates from the CLI registry and
 * `tests/test_site_cli_data.py` keeps current. Do not hand-write those
 * facts here.
 *
 * The vision statement is the one in `ROADMAP.md` / `docs/ARCHITECTURE.md`
 * / `README.md`; CLAUDE.md asks for it to be identical everywhere.
 */
export interface IndexPageProps {
  layouts: Layout[];
}

const REPO = "https://github.com/2AMLogic/klayout-tools";
const PYPI = "https://pypi.org/project/klayout-tools/";
const SAMPLE = "sky130_fd_sc_hd__inv_1";
const SAMPLE_URL = `https://klayout-tools.org/blocks/${SAMPLE}/${SAMPLE}.gds`;

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className="mt-14 scroll-mt-6">
      <h2 id={`${id}-heading`} className="font-mono text-[1.3rem] text-cyan">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Code({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <pre
      aria-label={label}
      className="my-4 overflow-x-auto rounded-lg border border-border bg-panel px-5 py-4 font-mono text-[0.85rem] leading-[1.7] text-fog"
    >
      {children}
    </pre>
  );
}

function Comment({ children }: { children: ReactNode }) {
  return <span className="text-fog-dim">{children}</span>;
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div className="mt-8">
      <h3 className="font-mono text-[1rem] text-orange">
        <span className="text-fog-dim">{n}.</span> {title}
      </h3>
      {children}
    </div>
  );
}

const prose = "my-[1.1rem] leading-[1.7]";

export function IndexPage({ layouts }: IndexPageProps) {
  const withData = layouts.filter((l) => l.status !== "no_artifacts").length;
  const { release, verb_count, mcp_tool_count, decks, groups } = cliData;

  return (
    <main className="mx-auto w-full max-w-[72rem] flex-1 px-6 py-12">
      <div className="max-w-[44rem]">
        <h1 className="font-mono text-[2.2rem] text-orange [text-shadow:0_0_6px_rgba(255,160,40,0.9),0_0_24px_rgba(255,160,40,0.4)]">
          klayout-tools
        </h1>
        <p className="mt-2 text-[0.85rem] tracking-[0.25em] text-cyan uppercase [text-shadow:0_0_6px_rgba(62,224,232,0.8),0_0_20px_rgba(62,224,232,0.3)]">
          IC layout for AI agents
        </p>
        <p className="mt-[1.2rem] flex flex-wrap gap-2 text-[0.8rem]">
          <a
            href={PYPI}
            className="inline-block rounded-full border border-orange px-[0.7rem] py-[0.15rem] text-orange no-underline hover:underline"
            data-testid="release-badge"
          >
            early alpha · v{release.version} on PyPI
          </a>
          <span className="inline-block rounded-full border border-border px-[0.7rem] py-[0.15rem] text-fog-dim">
            MIT · Python 3.10+
          </span>
        </p>

        <p className="mt-8 mb-[1.1rem] leading-[1.7]">
          Standalone tools that let AI agents parse, check, generate and verify chip
          layouts: GDSII and OASIS streams, DRC and LVS, extraction, simulation and a
          digital flow. Everything runs headless, and every command emits
          machine-readable JSON. You get a <code>klt</code> command line and an MCP
          server, built on <a href="https://www.klayout.de">KLayout</a>'s Python API.
        </p>
        <p className={prose}>
          It is the <a href="https://kicad-tools.org">kicad-tools</a> playbook one
          layer down the stack. There, agents take a design from schematic to routed
          PCB to fabrication package, and a gallery of real boards proves it. Here,
          the same pattern meets silicon.
        </p>
        <p className={prose}>
          The target capability: an agent can take a spec through one of three peer
          paths on an open PDK, unaided, with every step headless and JSON-contracted
          — analog (spec → schematic/generator → sized circuit → layout → DRC/LVS
          clean → extracted netlist → simulation-verified), digital (spec → RTL →
          synthesis → place-and-route → DRC/LVS clean → timing-closed), and
          mixed-signal (both paths plus the signoff seam between them).
        </p>
        <p className={`${prose} text-fog-dim`}>
          Early alpha: {verb_count} verbs exist today, and new verbs and JSON fields
          land between releases. The <a href={`${REPO}/blob/main/ROADMAP.md`}>roadmap</a>{" "}
          has the build order, and the{" "}
          <a href={`${REPO}/blob/main/docs/ARCHITECTURE.md`}>architecture</a> doc
          explains how the pieces fit.
        </p>
      </div>

      <div className="max-w-[44rem]">
        <Section id="quick-start" title="Quick start">
          <Step n={1} title="Install">
            <Code label="install">
              uv tool install klayout-tools{"\n"}
              <Comment># or: pip install klayout-tools</Comment>
            </Code>
            <p className={`${prose} text-[0.9rem] text-fog-dim`}>
              This puts <code>klt</code> on your <code>PATH</code> together with
              KLayout's headless Python module. You do not need a GUI. Optional extras
              are <code>[mcp]</code> for the MCP server and <code>[yield]</code> for
              the Monte Carlo yield verbs. Verbs that wrap an external engine (ngspice,
              xschem, Yosys, OpenROAD) need that engine installed, and <code>klt mom</code>{" "}
              still needs a source build for its Rust core. The{" "}
              <a href={`${REPO}#install`}>README</a> has the details.
            </p>
          </Step>

          <Step n={2} title="Run it on a real cell">
            <p className={`${prose} text-[0.9rem] text-fog-dim`}>
              This site serves the GDS for every gallery block. Here is the
              open-source SkyWater sky130 inverter:
            </p>
            <Code label="first session">
              curl -O {SAMPLE_URL}
              {"\n\n"}klt layers {SAMPLE}.gds{"            "}
              <Comment># layers and shape counts</Comment>
              {"\n"}klt cells {SAMPLE}.gds --top{"       "}
              <Comment># cell hierarchy</Comment>
              {"\n"}klt stats {SAMPLE}.gds --per-layer{" "}
              <Comment># area, density, polygons</Comment>
              {"\n"}klt drc {SAMPLE}.gds --deck sky130{" "}
              <Comment># status: clean, 0 violations</Comment>
              {"\n"}klt render {SAMPLE}.gds -o renders{" "}
              <Comment># per-layer PNGs, headless</Comment>
              {"\n\n"}
              <Comment># the same commands with --format json emit the JSON contract</Comment>
              {"\n"}klt drc {SAMPLE}.gds --deck sky130 --format json
            </Code>
            <p className={`${prose} text-[0.9rem] text-fog-dim`}>
              With <code>--format json</code>, every verb answers in one envelope:{" "}
              <code>schema_version</code>, a common error shape and documented exit
              codes. That envelope is the API (
              <a href={`${REPO}/blob/main/docs/json-contract.md`}>JSON contract</a>).
            </p>
          </Step>

          <Step n={3} title="Give it to an agent (MCP)">
            <Code label="MCP server">
              pip install 'klayout-tools[mcp]'
              {"\n\n"}
              <Comment># MCP client config (Claude Desktop, Claude Code, Cursor, …)</Comment>
              {"\n"}
              {`{
  "mcpServers": {
    "klayout-tools": {
      "command": "klt",
      "args": ["mcp", "serve", "--workspace-root", "/path/to/your/project"]
    }
  }
}`}
            </Code>
            <p className={`${prose} text-[0.9rem] text-fog-dim`}>
              <code>klt mcp serve</code> generates {mcp_tool_count} tools from the CLI
              registry, one for each leaf verb, named <code>klt_&lt;verb&gt;</code>. It
              runs each call as a subprocess, so the result is the same JSON the CLI
              prints. By default, path arguments that resolve outside the workspace root
              are rejected, and verbs that launch remote compute are hidden (
              <a href={`${REPO}/blob/main/docs/guides/mcp-server.md`}>MCP guide</a>).
            </p>
          </Step>

          <Step n={4} title="Check layouts in CI">
            <Code label="GitHub Action">
              <Comment># .github/workflows/layout.yml</Comment>
              {`
- uses: 2AMLogic/klayout-tools@v${release.version}
  with:
    layout: layout/my_block.gds
    verbs: drc,layout-metrics
    deck: sky130`}
            </Code>
            <p className={`${prose} text-[0.9rem] text-fog-dim`}>
              The composite Action installs <code>klt</code>, runs the verbs you list,
              and publishes a step summary plus JSON and render artifacts (
              <a href={`${REPO}/blob/main/docs/guides/github-action.md`}>Action guide</a>).
            </p>
          </Step>
        </Section>
      </div>

      <Section id="capabilities" title="Capabilities">
        <p className={`${prose} max-w-[44rem] text-fog-dim`}>
          {verb_count} verbs, each headless, each documented, each speaking the same
          JSON envelope. Built-in rule decks for open PDKs only:{" "}
          {decks.map((d, i) => (
            <span key={d}>
              {i > 0 && ", "}
              <code>{d}</code>
            </span>
          ))}
          . This list is generated from the CLI on <code>main</code>, which can be
          ahead of v{release.version}; <code>klt --help</code> lists what your install
          has.
        </p>
        <div
          className="mt-6 grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-5 max-[480px]:grid-cols-1"
          data-testid="capability-groups"
        >
          {groups.map((group) => (
            <section
              key={group.id}
              aria-label={group.title}
              className="rounded-lg border border-border bg-panel px-5 py-4"
            >
              <h3 className="font-mono text-[0.95rem] text-orange">
                {group.title}{" "}
                <span className="text-[0.8rem] text-fog-dim">{group.verbs.length}</span>
              </h3>
              <ul role="list" className="mt-3 list-none space-y-2 p-0 text-[0.85rem] leading-[1.5]">
                {group.verbs.map((verb) => (
                  <li key={verb.name}>
                    {verb.doc ? (
                      <a href={verb.doc} className="font-mono no-underline hover:underline">
                        klt {verb.name}
                      </a>
                    ) : (
                      <code className="font-mono text-cyan">klt {verb.name}</code>
                    )}
                    <span className="block text-fog-dim">{verb.summary}</span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </Section>

      <Section id="gallery" title="Gallery">
        <p className={`${prose} max-w-[44rem] text-fog-dim`}>
          Real layouts on open PDKs. Each page is built from the block's committed{" "}
          <code>klt layout-metrics</code> output and shows layer and cell stats,
          renders, and an in-browser GDS viewer.
        </p>
        <p className="text-[0.85rem] text-fog-dim">
          {layouts.length} block{layouts.length === 1 ? "" : "s"}
          {withData > 0 && ` · ${withData} with generated artifacts`}
        </p>

        {layouts.length === 0 && (
          <p className={`${prose} max-w-[44rem] text-fog-dim`}>
            No blocks discovered. Add blocks under <code>blocks/</code> and run{" "}
            <code>klt layout-metrics</code> to populate data.
          </p>
        )}

        <ul role="list" className="mt-5 grid list-none grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-5 p-0 max-[480px]:grid-cols-1">
          {layouts.map((layout) => (
            <li key={layout.slug} className="flex">
              <div className="w-full">
                <LayoutCard layout={layout} />
              </div>
            </li>
          ))}
        </ul>
      </Section>

      <Section id="docs" title="Docs">
        <ul role="list" className="mt-4 grid list-none grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-x-6 gap-y-2 p-0 text-[0.9rem] leading-[1.6]">
          <li><a href={`${REPO}/tree/main/docs/cli`}>CLI reference</a> <span className="text-fog-dim">— one page per verb</span></li>
          <li><a href={`${REPO}/blob/main/docs/json-contract.md`}>JSON contract</a> <span className="text-fog-dim">— envelope, errors, exit codes</span></li>
          <li><a href={`${REPO}/blob/main/docs/guides/mcp-server.md`}>MCP server</a> <span className="text-fog-dim">— client config, safety</span></li>
          <li><a href={`${REPO}/blob/main/docs/guides/github-action.md`}>GitHub Action</a> <span className="text-fog-dim">— inputs and outputs</span></li>
          <li><a href={`${REPO}/blob/main/docs/ARCHITECTURE.md`}>Architecture</a> <span className="text-fog-dim">— scope and layers</span></li>
          <li><a href={`${REPO}/blob/main/ROADMAP.md`}>Roadmap</a> <span className="text-fog-dim">— build order</span></li>
          <li><a href={`${REPO}/blob/main/CHANGELOG.md`}>Changelog</a> <span className="text-fog-dim">— v{release.version}, {release.date}</span></li>
          <li><a href={PYPI}>PyPI</a> <span className="text-fog-dim">— klayout-tools</span></li>
          <li><a href={`${REPO}/issues`}>Issues</a> <span className="text-fog-dim">— bugs and requests</span></li>
        </ul>
      </Section>
    </main>
  );
}
