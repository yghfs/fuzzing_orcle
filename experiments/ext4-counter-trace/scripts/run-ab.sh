#!/usr/bin/env bash
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
duration=${AB_DURATION_SECONDS:-1800}
common=(--mode single --workers 1 --duration-seconds "$duration" --segments 10000
  --segment-seconds 10 --max-batches 1 --syzkaller-calls 32 --guest-memory-mb 2048
  --cpus 2 --memory-mb 32 --max-trace-mb 64 --max-run-mb 8192 --min-free-mb 1024)
root=${AB_OUTPUT_ROOT:-"$project/results/ab-$(date +%Y%m%d-%H%M%S)"}
mkdir -p "$root"
run_one() {
  local label=$1; shift
  echo "[$(date --iso-8601=seconds)] A/B START $label" >&2
  local output
  set +e
  python3 "$project/scripts/campaign_runner.py" "${common[@]}" "$@" | tee "$root/$label.log"
  local rc=${PIPESTATUS[0]}
  set -e
  output=$(sed -n 's/^run_dir=//p' "$root/$label.log" | tail -1)
  test -n "$output"
  printf '%s\n' "$output" > "$root/$label.run-dir"
  cp "$output/campaign-report.json" "$root/$label-campaign-report.json"
  cp "$output/performance-report.json" "$root/$label-performance-report.json"
  printf '%s\n' "$rc" > "$root/$label-exit-code"
  echo "[$(date --iso-8601=seconds)] A/B END $label run=$output" >&2
}
run_one ordinary --resource-valid-every 0 --fresh-generate-every 5 --seed 20261016
run_one resource-valid --resource-valid-every 1 --fresh-generate-every 5 --seed 20261016
python3 "$project/scripts/ab_report.py" "$root" --output "$root/ab-report.json"
echo "$root/ab-report.md"
