#!/usr/bin/env bash
# Check the live inventory on main and the schedule. For proposed changes, an
# unavailable diff falls through to the live check just like a relevant diff.
set -eu

case "$GITHUB_EVENT_NAME" in
  pull_request|merge_group)
    if [[ -n "${CANARY_BASE_SHA:-}" && -n "${CANARY_HEAD_SHA:-}" ]] &&
        git diff --quiet "$CANARY_BASE_SHA...$CANARY_HEAD_SHA" -- \
          blocks/canaries.json scripts/ingest-canary.py scripts/ci-canary-manifest.sh \
          .github/workflows/canary-manifest.yml; then
      echo "Canary inventory inputs are unchanged. Skipping the live forge check."
      exit 0
    fi
    ;;
esac

exec uv run --locked --extra dev python scripts/ingest-canary.py --check-manifest
