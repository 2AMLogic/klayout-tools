#!/usr/bin/env bash
#
# deploy-site.sh — deploy the klayout-tools.org site to Cloudflare Pages.
#
# Mirrors kicad-tools/scripts/deploy-site.sh in spirit; the site is static
# (site/index.html), so there is no render/build pipeline yet. When the
# gallery lands (ROADMAP phase 4), the staging steps go here.
#
# Auth: scoped token, NOT wrangler OAuth —
#   source ~/.cloudflare/rjwalters/pages-rjwalters.env
# (personal account; the klayout-tools.org zone lives there, next to
# kicad-tools.org).

set -euo pipefail
cd "$(dirname "$0")/.."

: "${CLOUDFLARE_API_TOKEN:?source ~/.cloudflare/rjwalters/pages-rjwalters.env first}"
: "${CLOUDFLARE_ACCOUNT_ID:?source ~/.cloudflare/rjwalters/pages-rjwalters.env first}"

exec npx wrangler pages deploy site --project-name klayout-tools --branch main --commit-dirty=true
