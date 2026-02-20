#!/usr/bin/env bash
# Repeatedly triggers the backfill workflow until all windows are fetched.
# Requires: gh CLI installed and authenticated (brew install gh && gh auth login)

set -euo pipefail

REPO="bphelps1/orestar-dashboard"
WORKFLOW="backfill.yml"
START_YEAR="2017"
TOTAL_WINDOWS=954   # 477 weeks × 2 types (C + E)

run=0
while true; do
  run=$((run + 1))

  # Count how many windows are already recorded as fetched
  fetched=$(gh api "repos/$REPO/contents/data/fetched_windows.json" \
    --jq '.content' 2>/dev/null \
    | base64 -d 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo 0)

  echo ""
  echo "=== Run $run | Fetched so far: $fetched / $TOTAL_WINDOWS ==="

  if [ "$fetched" -ge "$TOTAL_WINDOWS" ]; then
    echo "All $TOTAL_WINDOWS windows fetched. Backfill complete!"
    break
  fi

  echo "Triggering workflow..."
  gh workflow run "$WORKFLOW" --repo "$REPO" -f start_year="$START_YEAR"

  # Wait a few seconds for the run to register, then find its ID
  sleep 10
  RUN_ID=$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId')
  echo "Monitoring run $RUN_ID..."

  # Poll until the run completes
  while true; do
    STATUS=$(gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion --jq '.status')
    if [ "$STATUS" = "completed" ]; then
      CONCLUSION=$(gh run view "$RUN_ID" --repo "$REPO" --json conclusion --jq '.conclusion')
      echo "Run $RUN_ID finished: $CONCLUSION"
      break
    fi
    echo "  status=$STATUS — waiting 30s..."
    sleep 30
  done

  # Brief pause before next run to let git push propagate
  sleep 15
done
