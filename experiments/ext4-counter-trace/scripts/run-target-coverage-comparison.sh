#!/usr/bin/env bash
set -uo pipefail

# Run the custom target-scoped feedback loop and an upstream syz-manager
# reference under one wall-clock budget, then emit a corpus-scope comparison.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ROOT_DIR=$(cd -- "$PROJECT_DIR/../.." && pwd)
RUNS_ROOT="$PROJECT_DIR/results"
CUSTOM_RUNNER=${FEEDBACK_COMPARISON_CUSTOM_RUNNER:-"$SCRIPT_DIR/run-campaign.sh"}
UPSTREAM_RUNNER=${FEEDBACK_COMPARISON_UPSTREAM_RUNNER:-"$SCRIPT_DIR/run-upstream-syzkaller-baseline.sh"}
DURATION_SECONDS=3600
TARGET_SYSCALLS=0
SEED=20261024
OUTPUT_DIR=""
ROOTFS="$ROOT_DIR/work/debian-clean-smalljournal.img"
FEEDBACK_MODE=pc
TARGET_SCOPE=ext4_jbd2
SCHEDULER_POLICY=rarity_fair
MUTATION_BUDGET_POLICY=fixed
CUSTOM_PORT=12883
UPSTREAM_HTTP_PORT=12900
RESOURCE_VALID_EVERY=4
FRESH_GENERATE_EVERY=7
STOP_GRACE_SECONDS=120
UPSTREAM_SEED_PROGRAM="$PROJECT_DIR/syzgen/seeds/upstream-ext4.syz"

usage() {
    cat <<'EOF'
Usage: run-target-coverage-comparison.sh [options]

Starts one custom target-scoped campaign and one upstream syz-manager run at
the same wall-clock budget. The final comparison uses custom admitted-corpus
target PCs and upstream manager-corpus raw PCs; all-sample custom coverage is
retained as a diagnostic metric.

Options:
  --duration-seconds N             Shared collection time (default: 3600)
  --target-syscalls N              Custom minimum syscall budget (default: 0)
  --seed N                         Custom deterministic seed; upstream records but cannot apply it
  --output DIR                     Comparison output directory
  --rootfs PATH                    Shared rootfs backing image
  --feedback-mode MODE             pc|pc_edge|pc_semantic|pc_edge_semantic (default: pc)
  --target-scope SCOPE             all|ext4_jbd2 (default: ext4_jbd2)
  --scheduler-policy POLICY        uniform_round_robin|rarity_fair (default: rarity_fair)
  --mutation-budget-policy POLICY  fixed|dynamic (default: fixed)
  --custom-port N                  Custom VM SSH port (default: 12883)
  --upstream-http-port N           Upstream manager HTTP port (default: 12900)
  --upstream-seed-program PATH     Upstream .syz seed (default: syzgen/seeds/upstream-ext4.syz)
EOF
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
while (($#)); do
    case "$1" in
        --duration-seconds|--target-syscalls|--seed|--output|--rootfs|--feedback-mode|--target-scope|\
        --scheduler-policy|--mutation-budget-policy|--custom-port|--upstream-http-port|--upstream-seed-program)
            (($# >= 2)) || { usage >&2; exit 2; }
            key=$1; value=$2
            case "$key" in
                --duration-seconds) DURATION_SECONDS=$value;;
                --target-syscalls) TARGET_SYSCALLS=$value;;
                --seed) SEED=$value;;
                --output) OUTPUT_DIR=$value;;
                --rootfs) ROOTFS=$value;;
                --feedback-mode) FEEDBACK_MODE=$value;;
                --target-scope) TARGET_SCOPE=$value;;
                --scheduler-policy) SCHEDULER_POLICY=$value;;
                --mutation-budget-policy) MUTATION_BUDGET_POLICY=$value;;
                --custom-port) CUSTOM_PORT=$value;;
                --upstream-http-port) UPSTREAM_HTTP_PORT=$value;;
                --upstream-seed-program) UPSTREAM_SEED_PROGRAM=$value;;
            esac
            shift 2;;
        -h|--help) usage; exit 0;;
        *) usage >&2; exit 2;;
    esac
done

for spec in "duration:$DURATION_SECONDS:1:43200" "target_syscalls:$TARGET_SYSCALLS:0:10000000" \
            "seed:$SEED:1:2147483647" "custom_port:$CUSTOM_PORT:1024:65535" \
            "upstream_http_port:$UPSTREAM_HTTP_PORT:1024:65535"; do
    IFS=: read -r name value minimum maximum <<< "$spec"
    is_uint "$value" && ((value >= minimum && value <= maximum)) || {
        echo "$name must be an integer in [$minimum,$maximum]" >&2; exit 2;
    }
done
(( DURATION_SECONDS <= 43200 && TARGET_SYSCALLS <= 10000000 )) || exit 2
[[ "$TARGET_SCOPE" == all || "$TARGET_SCOPE" == ext4_jbd2 ]] || { echo "invalid target scope" >&2; exit 2; }
[[ "$SCHEDULER_POLICY" == uniform_round_robin || "$SCHEDULER_POLICY" == rarity_fair ]] || { echo "invalid scheduler policy" >&2; exit 2; }
[[ "$MUTATION_BUDGET_POLICY" == fixed || "$MUTATION_BUDGET_POLICY" == dynamic ]] || { echo "invalid mutation budget policy" >&2; exit 2; }
[[ "$MUTATION_BUDGET_POLICY" != dynamic || "$SCHEDULER_POLICY" == rarity_fair ]] || { echo "dynamic budgets require rarity_fair" >&2; exit 2; }
[[ "$FEEDBACK_MODE" == pc || "$FEEDBACK_MODE" == pc_edge || "$FEEDBACK_MODE" == pc_semantic || "$FEEDBACK_MODE" == pc_edge_semantic ]] || { echo "invalid feedback mode" >&2; exit 2; }
[[ -f "$CUSTOM_RUNNER" && -r "$CUSTOM_RUNNER" && -f "$UPSTREAM_RUNNER" && -r "$UPSTREAM_RUNNER" ]] || {
    echo "comparison runners must be readable scripts" >&2; exit 2;
}
[[ -s "$ROOTFS" ]] || { echo "rootfs image missing: $ROOTFS" >&2; exit 2; }
[[ -s "$UPSTREAM_SEED_PROGRAM" ]] || { echo "upstream seed program missing: $UPSTREAM_SEED_PROGRAM" >&2; exit 2; }

if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$RUNS_ROOT/target-comparison-$(date +%Y%m%d-%H%M%S)"
fi
OUTPUT_DIR=$(mkdir -p -- "$OUTPUT_DIR" && cd -- "$OUTPUT_DIR" && pwd)
if [[ -e "$OUTPUT_DIR/comparison.json" ]]; then
    echo "refusing to overwrite existing comparison: $OUTPUT_DIR/comparison.json" >&2; exit 2
fi
for artifact in custom.log upstream.log upstream; do
    if [[ -e "$OUTPUT_DIR/$artifact" ]]; then
        echo "refusing to overwrite existing comparison artifact: $OUTPUT_DIR/$artifact" >&2; exit 2
    fi
done
if [[ "$CUSTOM_PORT" == "$UPSTREAM_HTTP_PORT" ]]; then
    echo "custom and upstream ports must differ" >&2; exit 2
fi
if command -v ss >/dev/null 2>&1; then
    for port in "$CUSTOM_PORT" "$UPSTREAM_HTTP_PORT"; do
        if ss -H -ltn "sport = :$port" | awk 'NF { found=1 } END { exit !found }'; then
            echo "port $port is already listening" >&2; exit 1
        fi
    done
fi

CUSTOM_LOG="$OUTPUT_DIR/custom.log"
UPSTREAM_LOG="$OUTPUT_DIR/upstream.log"
CUSTOM_PID=""
UPSTREAM_PID=""
stop_children() {
    for pid in "$CUSTOM_PID" "$UPSTREAM_PID"; do
        [[ -n "$pid" ]] || continue
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
}
force_children() {
    for pid in "$CUSTOM_PID" "$UPSTREAM_PID"; do
        [[ -n "$pid" ]] || continue
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done
}
trap 'stop_children' INT TERM HUP

ROOTFS_ABS=$(cd -- "$(dirname -- "$ROOTFS")" && pwd)/$(basename -- "$ROOTFS")
START_NS=$(date +%s%N)
printf 'comparison_start_ns=%s\nduration_seconds=%s\nseed=%s\nrootfs=%s\n' \
    "$START_NS" "$DURATION_SECONDS" "$SEED" "$ROOTFS_ABS" > "$OUTPUT_DIR/launch-metadata.txt"

setsid env EXT4_CT_IMAGE="$ROOTFS_ABS" bash "$UPSTREAM_RUNNER" --mode fuzzing --duration-seconds "$DURATION_SECONDS" \
    --seed "$SEED" --http-port "$UPSTREAM_HTTP_PORT" --output "$OUTPUT_DIR/upstream" \
    --seed-program "$UPSTREAM_SEED_PROGRAM" \
    --coverage-interval-seconds 60 --stop-grace-seconds "$STOP_GRACE_SECONDS" \
    >"$UPSTREAM_LOG" 2>&1 &
UPSTREAM_PID=$!
setsid bash "$CUSTOM_RUNNER" --rootfs-backing "$ROOTFS_ABS" --mode single \
    --segments 100000 --duration-seconds "$DURATION_SECONDS" --target-syscalls "$TARGET_SYSCALLS" \
    --segment-seconds 1 --max-batches 1 --batch-timeout 30 --boot-timeout 180 \
    --analysis-timeout 180 --workers 1 --memory-mb 0 --guest-memory-mb 1024 --cpus 1 \
    --buffer-kb 8192 --max-trace-mb 32 --max-run-mb 8192 --min-free-mb 1024 \
    --port "$CUSTOM_PORT" --seed "$SEED" --syzkaller-calls 16 \
    --resource-valid-every "$RESOURCE_VALID_EVERY" --fresh-generate-every "$FRESH_GENERATE_EVERY" \
    --scheduler-policy "$SCHEDULER_POLICY" --feedback-mode "$FEEDBACK_MODE" \
    --strict-kcov-feedback --mutation-budget-policy "$MUTATION_BUDGET_POLICY" \
    --target-scope "$TARGET_SCOPE" --scenario baseline \
    >"$CUSTOM_LOG" 2>&1 &
CUSTOM_PID=$!

custom_rc=0; upstream_rc=0
# Both runners share one wall-clock budget. Poll for the first child to
# disappear so an early failure cannot leave the other VM running unattended.
custom_done=0; upstream_done=0
while ((custom_done == 0 && upstream_done == 0)); do
    kill -0 "$CUSTOM_PID" 2>/dev/null || custom_done=1
    kill -0 "$UPSTREAM_PID" 2>/dev/null || upstream_done=1
    ((custom_done || upstream_done)) && break
    sleep 1
done
if ((custom_done)); then
    wait "$CUSTOM_PID" || custom_rc=$?
    if ((custom_rc != 0)) && ((upstream_done == 0)); then
        stop_children
    fi
fi
if ((upstream_done)); then
    wait "$UPSTREAM_PID" || upstream_rc=$?
    if ((upstream_rc != 0)) && ((custom_done == 0)); then
        stop_children
    fi
fi
if ((custom_done == 0)); then
    wait "$CUSTOM_PID" || custom_rc=$?
fi
if ((upstream_done == 0)); then
    wait "$UPSTREAM_PID" || upstream_rc=$?
fi
trap - INT TERM HUP

CUSTOM_RUN_DIR=$(sed -n 's/^run_dir=//p' "$CUSTOM_LOG" 2>/dev/null | tail -n 1)
if [[ -z "$CUSTOM_RUN_DIR" || ! -d "$CUSTOM_RUN_DIR" ]]; then
    echo "custom runner did not report a run directory" >&2
    force_children
    exit 1
fi
python3 "$SCRIPT_DIR/compare-target-coverage.py" \
    --custom-run "$CUSTOM_RUN_DIR" --upstream-run "$OUTPUT_DIR/upstream" \
    --target-scope "$TARGET_SCOPE" --output "$OUTPUT_DIR/comparison.json" \
    >"$OUTPUT_DIR/comparison.stdout" 2>"$OUTPUT_DIR/comparison.error"
comparison_rc=$?
printf 'custom_exit_code=%s\nupstream_exit_code=%s\ncomparison_exit_code=%s\ncustom_run=%s\n' \
    "$custom_rc" "$upstream_rc" "$comparison_rc" "$CUSTOM_RUN_DIR" > "$OUTPUT_DIR/status.txt"

# A failed runner must never be hidden by a successfully generated report.
if ((custom_rc != 0 || upstream_rc != 0 || comparison_rc != 0)); then
    exit 1
fi
exit 0
