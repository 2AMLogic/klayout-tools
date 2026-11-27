#!/usr/bin/env bash
# An unavailable diff falls through to the live check just like a relevant diff.
set -eu

case "$GITHUB_EVENT_NAME" in
  pull_request|merge_group)
    if [[ -n "${CANARY_BASE_SHA:-}" && -n "${CANARY_HEAD_SHA:-}" ]] &&
        git diff --quiet "$CANARY_BASE_SHA...$CANARY_HEAD_SHA" -- \
          blocks/canaries.json scripts/ingest-canary.py scripts/ci-canary-manifest.sh \
          .github/workflows/canary-manifest.yml; then
      # Operator-approved exception to CI principles 6 and 11 (#7876). Unrelated
      # PRs and merge groups still run the deterministic manifest tests. Main CI
      # and the daily inventory workflow provide the live coverage under rule 10.
      echo "Canary inventory inputs are unchanged. Skipping the live forge check."
      exit 0
    fi
    ;;
esac

exec uv run --locked --extra dev python scripts/ingest-canary.py --check-manifest
