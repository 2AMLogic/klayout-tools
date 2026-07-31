/**
 * Shared HTML templating for the SSR dev middleware (`vite.config.ts`) and
 * the build-time prerender pass (`scripts/prerender.mjs`) — both inject a
 * server-rendered page + its hydration data into a copy of the same Vite
 * app-shell template (`index.html` in dev, the client-built
 * `dist/index.html` at build time), the same way, so dev and production
 * output stay behaviorally identical (issue #92).
 */

/**
 * @param {string} template Vite app-shell HTML with a `<div id="root"></div>`
 *   mount point and an entry `<script type="module">` tag already present.
 * @param {{ appHtml: string, title: string, description?: string, data: unknown }} page
 * @returns {string}
 */
export function injectPage(template, { appHtml, title, description, data }) {
  let html = template;

  // `[^<]*` (not `.*?`) so this can never span past an unrelated `<title>`-
  // looking substring inside an HTML comment elsewhere in the template.
  html = html.replace(/<title>[^<]*<\/title>/, `<title>${escapeHtml(title)}</title>`);

  const metaDescriptionRe = /\s*<meta\s+name="description"[^>]*>\n?/;
  if (description) {
    const tag = `<meta name="description" content="${escapeHtml(description)}" />`;
    html = metaDescriptionRe.test(html)
      ? html.replace(metaDescriptionRe, `\n    ${tag}\n`)
      : html.replace("</head>", `    ${tag}\n  </head>`);
  } else {
    html = html.replace(metaDescriptionRe, "\n");
  }

  html = html.replace('<div id="root"></div>', `<div id="root">${appHtml}</div>`);

  const dataScript = `<script>window.__KLT_DATA__ = ${JSON.stringify(data).replace(/</g, "\\u003c")};</script>`;
  html = html.replace("</body>", `    ${dataScript}\n  </body>`);

  return html;
}

/** @param {string} str */
function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
