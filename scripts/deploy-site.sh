#!/usr/bin/env bash
#
# deploy-site.sh — build and deploy the klayout-tools.org gallery site to
# Cloudflare Pages.
#
# Mirrors kicad-tools/scripts/deploy-site.sh in spirit (issue #65,
# rjwalters/kicad-tools#3688): the site is a Vite + React project
# (site/package.json; migrated from Astro in #92), so the deploy target is
# the built site/dist/ output — a fully static prerendered tree, no server
# runtime required.
#
# Pipeline:
#   1. build  -- `npm --prefix site ci && npm --prefix site run build`,
#                producing site/dist/.
#   2. deploy -- `wrangler pages deploy site/dist` (skipped under
#                --no-deploy).
#
# Auth stays as-is from the pre-gallery script: a scoped API token, NOT
# wrangler OAuth --
#   source ~/.cloudflare/rjwalters/pages-rjwalters.env
# (personal account; the klayout-tools.org zone lives there, next to
# kicad-tools.org).
#
# Usage:
#   scripts/deploy-site.sh [--no-deploy] [--help]
#
#   --no-deploy   Build the site only (site/dist/); skip the wrangler
#                 deploy. Useful for local verification without touching
#                 Cloudflare. Does not require Cloudflare credentials.
#   --help        Show this help and exit.
#
# Out of scope: regenerating blocks/*/output/layout.json or renders — that's
# the content pipeline (#62). This script only builds and deploys whatever
# is already checked into blocks/ and site/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

info() { printf '\033[0;34m[deploy]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; }

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" \
    | sed -e 's/^# \{0,1\}//' -e '/^set -euo pipefail/d'
}

NO_DEPLOY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-deploy) NO_DEPLOY=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)
      err "Unknown argument: $1"
      echo "Run '$0 --help' for usage." >&2
      exit 2
      ;;
  esac
  shift
done

cd "${REPO_ROOT}"

if ! command -v npm >/dev/null 2>&1; then
  err "'npm' (Node.js) not found — required to build the site. Install Node 22+."
  exit 1
fi

# --- Step 1: build the site --------------------------------------------------
info "Step 1/2: building the site (npm --prefix site ci && run build)"
npm --prefix site ci
npm --prefix site run build
info "Site built to site/dist/."

# --- Step 2: deploy ----------------------------------------------------------
if [ "${NO_DEPLOY}" -eq 1 ]; then
  info "Step 2/2: --no-deploy set; skipping Cloudflare Pages deploy."
  info "Done. Build is in site/dist/ (not deployed)."
  exit 0
fi

: "${CLOUDFLARE_API_TOKEN:?source ~/.cloudflare/rjwalters/pages-rjwalters.env first}"
: "${CLOUDFLARE_ACCOUNT_ID:?source ~/.cloudflare/rjwalters/pages-rjwalters.env first}"

# The guards above only verify the *shell* variables are set — they say
# nothing about whether they are exported, so the child `npx wrangler`
# process below may not inherit them and would silently fall back to
# cached OAuth (wrong account). Export explicitly.
export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID

# Identity guard (mirrors kicad-tools/scripts/deploy-site.sh's
# assert_cloudflare_account, adapted for API-token auth): refuse to deploy
# unless CLOUDFLARE_ACCOUNT_ID matches the expected personal account. This
# is an account identifier, not a secret — the real secret is
# CLOUDFLARE_API_TOKEN, which is never logged or hardcoded.
EXPECTED_CLOUDFLARE_ACCOUNT_ID="${EXPECTED_CLOUDFLARE_ACCOUNT_ID:-251e6e8626d921603fdc3f0d75576bc6}"
if [ "${CLOUDFLARE_ACCOUNT_ID}" != "${EXPECTED_CLOUDFLARE_ACCOUNT_ID}" ]; then
  err "WRONG Cloudflare account! Refusing to deploy."
  err "  CLOUDFLARE_ACCOUNT_ID: ${CLOUDFLARE_ACCOUNT_ID}"
  err "  expected:             ${EXPECTED_CLOUDFLARE_ACCOUNT_ID}"
  exit 1
fi

info "Step 2/2: deploying site/dist/ to Cloudflare Pages (branch main)."
exec npx wrangler pages deploy site/dist --project-name klayout-tools --branch main --commit-dirty=true
