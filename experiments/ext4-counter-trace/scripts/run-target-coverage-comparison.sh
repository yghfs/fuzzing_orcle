#!/usr/bin/env bash
set -uo pipefail

# Run native syz-manager, a PC-only ablation, and the full custom feedback loop
# against one regular ext4 image under a shared wall-clock budget.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ROOT_DIR=$(cd -- "$PROJECT_DIR/../.." && pwd)
RUNS_ROOT="$PROJECT_DIR/results"
CUSTOM_RUNNER=${FEEDBACK_COMPARISON_CUSTOM_RUNNER:-"$SCRIPT_DIR/run-campaign.sh"}
UPSTREAM_RUNNER=${FEEDBACK_COMPARISON_UPSTREAM_RUNNER:-"$SCRIPT_DIR/run-upstream-syzkaller-baseline.sh"}
UPSTREAM_MANAGER=${UPSTREAM_SYZ_MANAGER:-"$ROOT_DIR/tools/src/syzkaller-master/bin/syz-manager"}
UPSTREAM_SYZKALLER=${UPSTREAM_SYZKALLER:-"$ROOT_DIR/tools/src/syzkaller-master"}
KERNEL_OBJ=${EXT4_CT_KERNEL_OBJ:-"$PROJECT_DIR/build/linux-counter-trace"}
KERNEL=${EXT4_CT_KERNEL:-"$KERNEL_OBJ/arch/x86/boot/bzImage"}
VMLINUX=${EXT4_CT_VMLINUX:-"$KERNEL_OBJ/vmlinux"}
KERNEL_SRC=${EXT4_CT_KERNEL_SRC:-"$PROJECT_DIR/dev/linux-6.12.1"}
SSHKEY=${EXT4_CT_SSHKEY:-"$ROOT_DIR/work/debian.id_rsa"}
DURATION_SECONDS=3600
TARGET_SYSCALLS=0
SEED=20261024
OUTPUT_DIR=""
ROOTFS="$ROOT_DIR/work/debian-clean-smalljournal.img"
FEEDBACK_MODE=pc_edge_semantic
TARGET_SCOPE=ext4_jbd2
SCHEDULER_POLICY=rarity_fair
MUTATION_BUDGET_POLICY=fixed
CUSTOM_PORT=12883
PC_ONLY_PORT=12884
UPSTREAM_HTTP_PORT=12900
RESOURCE_VALID_EVERY=4
FRESH_GENERATE_EVERY=7
STOP_GRACE_SECONDS=120
UPSTREAM_SEED_PROGRAM="$PROJECT_DIR/syzgen/seeds/upstream-ext4.syz"
RANDOM_FAULT_PROBABILITY_PPM=1000

usage() {
    cat <<'EOF'
Usage: run-target-coverage-comparison.sh [options]

Starts three concurrent arms: native syz-manager, custom pc_only, and custom
full feedback (pc_edge, pc_semantic, or pc_edge_semantic). All use one generated
regular ext4 image and the same rootfs, kernel, duration, and custom settings.

Options:
  --duration-seconds N             Shared collection time (default: 3600)
  --target-syscalls N              Custom minimum syscall budget (default: 0)
  --seed N                         Custom deterministic seed; upstream records but cannot apply it
  --output DIR                     Comparison output directory
  --rootfs PATH                    Shared rootfs backing image
  --feedback-mode MODE             pc_edge|pc_semantic|pc_edge_semantic (default: pc_edge_semantic)
  --target-scope SCOPE             all|ext4_jbd2 (default: ext4_jbd2)
  --scheduler-policy POLICY        uniform_round_robin|rarity_fair (default: rarity_fair)
  --mutation-budget-policy POLICY  fixed|dynamic (default: fixed)
  --custom-port N                  Custom VM SSH port (default: 12883)
  --pc-only-port N                 PC-only custom VM SSH port (default: 12884)
  --upstream-http-port N           Upstream manager HTTP port (default: 12900)
  --upstream-seed-program PATH     Upstream .syz seed (default: syzgen/seeds/upstream-ext4.syz)
  --random-fault-probability-ppm N Per eligible ext4 hook call, shared by both custom arms (default: 1000)
EOF
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
while (($#)); do
    case "$1" in
        --duration-seconds|--target-syscalls|--seed|--output|--rootfs|--feedback-mode|--target-scope|\
        --random-fault-probability-ppm|\
        --scheduler-policy|--mutation-budget-policy|--custom-port|--pc-only-port|--upstream-http-port|--upstream-seed-program)
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
                --random-fault-probability-ppm) RANDOM_FAULT_PROBABILITY_PPM=$value;;
                --scheduler-policy) SCHEDULER_POLICY=$value;;
                --mutation-budget-policy) MUTATION_BUDGET_POLICY=$value;;
                --custom-port) CUSTOM_PORT=$value;;
                --pc-only-port) PC_ONLY_PORT=$value;;
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
            "pc_only_port:$PC_ONLY_PORT:1024:65535" \
            "upstream_http_port:$UPSTREAM_HTTP_PORT:1024:65535" \
            "random_fault_probability_ppm:$RANDOM_FAULT_PROBABILITY_PPM:1:1000000"; do
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
[[ "$FEEDBACK_MODE" == pc_edge || "$FEEDBACK_MODE" == pc_semantic || "$FEEDBACK_MODE" == pc_edge_semantic ]] || { echo "full feedback mode must be pc_edge, pc_semantic, or pc_edge_semantic" >&2; exit 2; }
[[ -f "$CUSTOM_RUNNER" && -r "$CUSTOM_RUNNER" && -f "$UPSTREAM_RUNNER" && -r "$UPSTREAM_RUNNER" ]] || {
    echo "comparison runners must be readable scripts" >&2; exit 2;
}
[[ -s "$ROOTFS" ]] || { echo "rootfs image missing: $ROOTFS" >&2; exit 2; }
[[ -s "$UPSTREAM_SEED_PROGRAM" ]] || { echo "upstream seed program missing: $UPSTREAM_SEED_PROGRAM" >&2; exit 2; }
[[ -x "$UPSTREAM_MANAGER" ]] || { echo "upstream syz-manager missing: $UPSTREAM_MANAGER" >&2; exit 2; }
[[ -d "$UPSTREAM_SYZKALLER" ]] || { echo "upstream syzkaller tree missing: $UPSTREAM_SYZKALLER" >&2; exit 2; }
[[ -s "$KERNEL" && -s "$VMLINUX" && -d "$KERNEL_SRC" ]] || {
    echo "counter-trace kernel build or source tree is incomplete" >&2; exit 2;
}
[[ -r "$SSHKEY" ]] || { echo "SSH key missing: $SSHKEY" >&2; exit 2; }
for tool in qemu-img qemu-system-x86_64 python3 gcc mke2fs debugfs e2fsck ssh scp nm; do
    command -v "$tool" >/dev/null 2>&1 || { echo "required tool missing: $tool" >&2; exit 2; }
done
if [[ -n "${FUZZING_ORCLE_GO:-}" ]]; then
    command -v "$FUZZING_ORCLE_GO" >/dev/null 2>&1 || [[ -x "$FUZZING_ORCLE_GO" ]] || {
        echo "configured Go tool missing: $FUZZING_ORCLE_GO" >&2; exit 2;
    }
elif ! command -v go >/dev/null 2>&1 && [[ ! -x "$ROOT_DIR/tools/go126/bin/go1.26.0" ]]; then
    echo "Go toolchain missing" >&2; exit 2
fi
python3 "$PROJECT_DIR/scripts/source_audit.py" --require-build || {
    echo "reviewed source or audited kernel build binding check failed" >&2; exit 2;
}

if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$RUNS_ROOT/target-comparison-$(date +%Y%m%d-%H%M%S)"
fi
OUTPUT_DIR=$(mkdir -p -- "$OUTPUT_DIR" && cd -- "$OUTPUT_DIR" && pwd)
if [[ -e "$OUTPUT_DIR/comparison.json" ]]; then
    echo "refusing to overwrite existing comparison: $OUTPUT_DIR/comparison.json" >&2; exit 2
fi
for artifact in pc_only.log full_feedback.log upstream.log upstream launch-metadata.txt status.txt comparison.stdout comparison.error seed; do
    if [[ -e "$OUTPUT_DIR/$artifact" ]]; then
        echo "refusing to overwrite existing comparison artifact: $OUTPUT_DIR/$artifact" >&2; exit 2
    fi
done
if [[ "$CUSTOM_PORT" == "$UPSTREAM_HTTP_PORT" || "$CUSTOM_PORT" == "$PC_ONLY_PORT" || "$PC_ONLY_PORT" == "$UPSTREAM_HTTP_PORT" ]]; then
    echo "custom, pc-only, and upstream ports must differ" >&2; exit 2
fi
if command -v ss >/dev/null 2>&1; then
    for port in "$CUSTOM_PORT" "$PC_ONLY_PORT" "$UPSTREAM_HTTP_PORT"; do
        if ss -H -ltn "sport = :$port" | awk 'NF { found=1 } END { exit !found }'; then
            echo "port $port is already listening" >&2; exit 1
        fi
    done
fi

PC_LOG="$OUTPUT_DIR/pc_only.log"
FULL_LOG="$OUTPUT_DIR/full_feedback.log"
UPSTREAM_LOG="$OUTPUT_DIR/upstream.log"
: > "$PC_LOG"
: > "$FULL_LOG"
: > "$UPSTREAM_LOG"
PC_PID=""
FULL_PID=""
UPSTREAM_PID=""
stop_children() {
    for pid in "$PC_PID" "$FULL_PID" "$UPSTREAM_PID"; do
        [[ -n "$pid" ]] || continue
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
}
force_children() {
    for pid in "$PC_PID" "$FULL_PID" "$UPSTREAM_PID"; do
        [[ -n "$pid" ]] || continue
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done
}
trap 'stop_children' INT TERM HUP

ROOTFS_ABS=$(cd -- "$(dirname -- "$ROOTFS")" && pwd)/$(basename -- "$ROOTFS")
SEED_DIR="$OUTPUT_DIR/seed"
SEED_IMAGE="$SEED_DIR/regular.raw"
SEED_PROGRAM="$SEED_DIR/upstream-ext4.syz"
SEED_METADATA="$SEED_DIR/metadata.json"
mkdir -p -- "$SEED_DIR"
python3 "$PROJECT_DIR/scripts/prepare-comparison-ext4-seed.py" \
    --seed "$SEED" --image "$SEED_IMAGE" --template "$UPSTREAM_SEED_PROGRAM" \
    --program "$SEED_PROGRAM" --metadata "$SEED_METADATA" > "$SEED_DIR/prepare.stdout" \
    2> "$SEED_DIR/prepare.stderr" || {
        echo "failed to prepare the shared regular ext4 seed image" >&2
        exit 1
    }
START_NS=$(date +%s%N)
printf 'comparison_start_ns=%s\nduration_seconds=%s\nseed=%s\nrootfs=%s\ntarget_ext4_image=%s\n' \
    "$START_NS" "$DURATION_SECONDS" "$SEED" "$ROOTFS_ABS" "$SEED_IMAGE" > "$OUTPUT_DIR/launch-metadata.txt"
printf 'pc_only_feedback_mode=pc\nfull_feedback_mode=%s\ntarget_scope=%s\ntarget_kind=regular\nscheduler_policy=%s\nmutation_budget_policy=%s\nrandom_fault_injection=true\nrandom_fault_probability_ppm=%s\nfault_isolation=true\npc_only_port=%s\nfull_feedback_port=%s\nupstream_http_port=%s\npc_only_vcpu=1\npc_only_memory_mb=1024\nfull_feedback_vcpu=1\nfull_feedback_memory_mb=1024\nupstream_vcpu=1\nupstream_memory_mb=1024\nqemu_accelerator=tcg,thread=multi\nqemu_cpu=qemu64\nparallel_arms=3\n' \
    "$FEEDBACK_MODE" "$TARGET_SCOPE" "$SCHEDULER_POLICY" "$MUTATION_BUDGET_POLICY" \
    "$RANDOM_FAULT_PROBABILITY_PPM" "$PC_ONLY_PORT" "$CUSTOM_PORT" "$UPSTREAM_HTTP_PORT" >> "$OUTPUT_DIR/launch-metadata.txt"

setsid env EXT4_CT_IMAGE="$ROOTFS_ABS" bash "$UPSTREAM_RUNNER" --mode fuzzing --duration-seconds "$DURATION_SECONDS" \
    --seed "$SEED" --http-port "$UPSTREAM_HTTP_PORT" --output "$OUTPUT_DIR/upstream" \
    --seed-program "$SEED_PROGRAM" --target-ext4-image "$SEED_IMAGE" \
    --coverage-interval-seconds 60 --stop-grace-seconds "$STOP_GRACE_SECONDS" \
    >"$UPSTREAM_LOG" 2>&1 &
UPSTREAM_PID=$!
setsid bash "$CUSTOM_RUNNER" --rootfs-backing "$ROOTFS_ABS" --mode single \
    --segments 100000 --duration-seconds "$DURATION_SECONDS" --target-syscalls "$TARGET_SYSCALLS" \
    --segment-seconds 1 --max-batches 1 --batch-timeout 30 --boot-timeout 180 \
    --analysis-timeout 180 --workers 1 --memory-mb 0 --guest-memory-mb 1024 --cpus 1 \
    --buffer-kb 8192 --max-trace-mb 32 --max-run-mb 8192 --min-free-mb 1024 \
    --port "$PC_ONLY_PORT" --seed "$SEED" --syzkaller-calls 16 \
    --resource-valid-every "$RESOURCE_VALID_EVERY" --fresh-generate-every "$FRESH_GENERATE_EVERY" \
    --scheduler-policy "$SCHEDULER_POLICY" --feedback-mode pc \
    --strict-kcov-feedback --mutation-budget-policy "$MUTATION_BUDGET_POLICY" \
    --random-fault-injection --random-fault-probability-ppm "$RANDOM_FAULT_PROBABILITY_PPM" --fault-isolation \
    --target-scope "$TARGET_SCOPE" --effect-aware-admission --target-kind regular --comparison-seed-image "$SEED_IMAGE" \
    --scenario baseline >"$PC_LOG" 2>&1 &
PC_PID=$!
setsid bash "$CUSTOM_RUNNER" --rootfs-backing "$ROOTFS_ABS" --mode single \
    --segments 100000 --duration-seconds "$DURATION_SECONDS" --target-syscalls "$TARGET_SYSCALLS" \
    --segment-seconds 1 --max-batches 1 --batch-timeout 30 --boot-timeout 180 \
    --analysis-timeout 180 --workers 1 --memory-mb 0 --guest-memory-mb 1024 --cpus 1 \
    --buffer-kb 8192 --max-trace-mb 32 --max-run-mb 8192 --min-free-mb 1024 \
    --port "$CUSTOM_PORT" --seed "$SEED" --syzkaller-calls 16 \
    --resource-valid-every "$RESOURCE_VALID_EVERY" --fresh-generate-every "$FRESH_GENERATE_EVERY" \
    --scheduler-policy "$SCHEDULER_POLICY" --feedback-mode "$FEEDBACK_MODE" \
    --strict-kcov-feedback --mutation-budget-policy "$MUTATION_BUDGET_POLICY" \
    --random-fault-injection --random-fault-probability-ppm "$RANDOM_FAULT_PROBABILITY_PPM" --fault-isolation \
    --target-scope "$TARGET_SCOPE" --effect-aware-admission --target-kind regular --comparison-seed-image "$SEED_IMAGE" \
    --scenario baseline >"$FULL_LOG" 2>&1 &
FULL_PID=$!

printf 'comparison_output=%s\npc_only_log=%s\nfull_feedback_log=%s\nupstream_log=%s\n' \
    "$OUTPUT_DIR" "$PC_LOG" "$FULL_LOG" "$UPSTREAM_LOG"
printf 'All three arms are running in parallel for %s seconds. Follow logs with:\n' "$DURATION_SECONDS"
printf '  tail -F %q %q %q\n' "$PC_LOG" "$FULL_LOG" "$UPSTREAM_LOG"
printf 'Press Ctrl-C to request graceful shutdown of both runs.\n'
printf 'The report records image identity, successful target-ext4 operations, and remaining syscall-set differences.\n'

# Keep every arm supervised; an early failure stops peers and preserves artifacts.
pc_rc=0; full_rc=0; upstream_rc=0
pc_done=0; full_done=0; upstream_done=0; first_failure=0
while ((pc_done == 0 || full_done == 0 || upstream_done == 0)); do
    finished_pid=""
    wait -n -p finished_pid
    wait_rc=$?
    if [[ -z "$finished_pid" ]]; then
        continue
    fi
    if [[ "$finished_pid" == "$PC_PID" ]]; then
        pc_done=1
        pc_rc=$wait_rc
    elif [[ "$finished_pid" == "$FULL_PID" ]]; then
        full_done=1
        full_rc=$wait_rc
    elif [[ "$finished_pid" == "$UPSTREAM_PID" ]]; then
        upstream_done=1
        upstream_rc=$wait_rc
    fi
    if ((wait_rc != 0 && first_failure == 0)); then
        first_failure=1
        stop_children
    fi
done
trap - INT TERM HUP

PC_RUN_DIR=$(sed -n 's/^run_dir=//p' "$PC_LOG" 2>/dev/null | tail -n 1)
FULL_RUN_DIR=$(sed -n 's/^run_dir=//p' "$FULL_LOG" 2>/dev/null | tail -n 1)
if [[ -z "$PC_RUN_DIR" || ! -d "$PC_RUN_DIR" || -z "$FULL_RUN_DIR" || ! -d "$FULL_RUN_DIR" ]]; then
    echo "one or both custom runners did not report a run directory" >&2
    force_children
    exit 1
fi
python3 "$SCRIPT_DIR/validate-target-ext4-operations.py" --campaign "$PC_RUN_DIR" \
    --output "$OUTPUT_DIR/pc-only-target-ext4-operations.json" \
    >"$OUTPUT_DIR/pc-only-target-ext4-operations.stdout" 2>"$OUTPUT_DIR/pc-only-target-ext4-operations.error"
pc_validation_rc=$?
python3 "$SCRIPT_DIR/validate-target-ext4-operations.py" --campaign "$FULL_RUN_DIR" \
    --output "$OUTPUT_DIR/full-feedback-target-ext4-operations.json" \
    >"$OUTPUT_DIR/full-feedback-target-ext4-operations.stdout" 2>"$OUTPUT_DIR/full-feedback-target-ext4-operations.error"
full_validation_rc=$?
python3 "$SCRIPT_DIR/compare-target-coverage.py" \
    --custom-run "$FULL_RUN_DIR" --upstream-run "$OUTPUT_DIR/upstream" \
    --pc-only-run "$PC_RUN_DIR" --full-feedback-run "$FULL_RUN_DIR" \
    --pc-only-operations "$OUTPUT_DIR/pc-only-target-ext4-operations.json" \
    --full-feedback-operations "$OUTPUT_DIR/full-feedback-target-ext4-operations.json" \
    --full-feedback-mode "$FEEDBACK_MODE" \
    --target-scope "$TARGET_SCOPE" --output "$OUTPUT_DIR/comparison.json" \
    >"$OUTPUT_DIR/comparison.stdout" 2>"$OUTPUT_DIR/comparison.error"
comparison_rc=$?
printf 'pc_only_exit_code=%s\nfull_feedback_exit_code=%s\nupstream_exit_code=%s\npc_only_operation_validation_exit_code=%s\nfull_operation_validation_exit_code=%s\ncomparison_exit_code=%s\npc_only_run=%s\nfull_feedback_run=%s\n' \
    "$pc_rc" "$full_rc" "$upstream_rc" "$pc_validation_rc" "$full_validation_rc" \
    "$comparison_rc" "$PC_RUN_DIR" "$FULL_RUN_DIR" > "$OUTPUT_DIR/status.txt"

# A failed runner must never be hidden by a successfully generated report.
if ((pc_rc != 0 || full_rc != 0 || upstream_rc != 0 || pc_validation_rc != 0 || \
     full_validation_rc != 0 || comparison_rc != 0)); then
    exit 1
fi
exit 0
