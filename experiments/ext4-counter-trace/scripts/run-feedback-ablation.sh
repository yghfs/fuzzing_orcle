#!/usr/bin/env bash
set -uo pipefail

# Four custom feedback modes with a shared initial ext4 image.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ROOT_DIR=$(cd -- "$PROJECT_DIR/../.." && pwd)
source "$ROOT_DIR/tools/kernel-paths.sh"
RUNNER=${FEEDBACK_ABLATION_RUNNER:-"$SCRIPT_DIR/run-campaign.sh"}
ARM_RUNNER="$SCRIPT_DIR/run-feedback-ablation-arm.sh"
SEED_PREPARER="$SCRIPT_DIR/prepare-comparison-ext4-seed.py"
SUMMARIZER="$SCRIPT_DIR/summarize-feedback-ablation.py"
ROOTFS="$FUZZING_ORCLE_ROOTFS"
DURATION_SECONDS=7200
SEED=20261120
OUTPUT_DIR=""
BASE_PORT=12481
SEGMENTS=100000
TARGET_SYSCALLS=0
TARGET_SCOPE=ext4_jbd2
BATCH_TIMEOUT=90
FAIL_FAST=0
MODES=(pc pc_edge pc_semantic pc_edge_semantic)

usage() {
    cat <<'EOF'
Usage: run-feedback-ablation.sh [options]

Run four custom arms concurrently: pc, pc_edge, pc_semantic, pc_edge_semantic.
All arms share seed, rootfs, kernel, regular ext4 starting image and workload
settings.

Options:
  --duration-seconds N  Collection time per arm (default: 7200)
  --seed N              Shared deterministic campaign/image seed
  --output DIR          Output directory for arm logs and ablation report
  --rootfs PATH         Shared rootfs backing image
  --base-port N         Four consecutive SSH ports (default: 12481)
  --segments N          Hard segment cap (default: 100000)
  --target-syscalls N   Optional verified-syscall stop threshold (default: 0)
  --target-scope SCOPE  all|ext4_jbd2 (default: ext4_jbd2)
  --batch-timeout N     Maximum guest workload time per batch in seconds (default: 90; range: 1..300)
  --fail-fast           Stop remaining arms after the first arm failure (default: let them finish)
  --help                Show this help

Example:
  bash experiments/ext4-counter-trace/scripts/run-feedback-ablation.sh \
    --duration-seconds 7200 --seed 20261120 \
    --output experiments/ext4-counter-trace/results/feedback-ablation-20261120

Live output is prefixed by arm and saved as <mode>.log. By default, a failed
arm does not stop the other arms; the overall run still fails validation.
Press Ctrl-C once to request graceful shutdown of all four arms.
EOF
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
while (($#)); do
    case "$1" in
        --duration-seconds|--seed|--output|--rootfs|--base-port|--segments|--target-syscalls|--target-scope|--batch-timeout)
            (($# >= 2)) || { usage >&2; exit 2; }
            key=$1 value=$2
            case "$key" in
                --duration-seconds) DURATION_SECONDS=$value ;;
                --seed) SEED=$value ;;
                --output) OUTPUT_DIR=$value ;;
                --rootfs) ROOTFS=$value ;;
                --base-port) BASE_PORT=$value ;;
                --segments) SEGMENTS=$value ;;
                --target-syscalls) TARGET_SYSCALLS=$value ;;
                --target-scope) TARGET_SCOPE=$value ;;
                --batch-timeout) BATCH_TIMEOUT=$value ;;
            esac
            shift 2 ;;
        --fail-fast) FAIL_FAST=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

for spec in "duration:$DURATION_SECONDS:1:43200" "seed:$SEED:1:2147483647" \
            "base_port:$BASE_PORT:1024:65532" "segments:$SEGMENTS:1:100000" \
            "target_syscalls:$TARGET_SYSCALLS:0:10000000" \
            "batch_timeout:$BATCH_TIMEOUT:1:300"; do
    IFS=: read -r name value minimum maximum <<< "$spec"
    is_uint "$value" && ((value >= minimum && value <= maximum)) || {
        echo "$name must be an integer in [$minimum,$maximum]" >&2; exit 2;
    }
done
[[ "$TARGET_SCOPE" == all || "$TARGET_SCOPE" == ext4_jbd2 ]] || {
    echo "target-scope must be all or ext4_jbd2" >&2; exit 2;
}

KERNEL_OBJ=${EXT4_CT_KERNEL_OBJ:-"$FUZZING_ORCLE_KERNEL_OBJ"}
KERNEL=${EXT4_CT_KERNEL:-"$KERNEL_OBJ/arch/x86/boot/bzImage"}
VMLINUX=${EXT4_CT_VMLINUX:-"$KERNEL_OBJ/vmlinux"}
KERNEL_SRC=${EXT4_CT_KERNEL_SRC:-"$FUZZING_ORCLE_KERNEL"}
SSHKEY=${EXT4_CT_SSHKEY:-"$ROOT_DIR/work/debian.id_rsa"}
SEED_TEMPLATE="$PROJECT_DIR/syzgen/seeds/upstream-ext4.syz"
[[ -r "$RUNNER" && -x "$ARM_RUNNER" && -r "$SUMMARIZER" && -r "$SEED_PREPARER" ]] || {
    echo "runner, arm helper, summarizer, or seed preparer missing" >&2; exit 2;
}
[[ -s "$ROOTFS" ]] || { echo "rootfs image missing: $ROOTFS" >&2; exit 2; }
[[ -s "$KERNEL" && -s "$VMLINUX" && -d "$KERNEL_SRC" ]] || {
    echo "counter-trace kernel build or source tree is incomplete" >&2; exit 2;
}
[[ -r "$SSHKEY" ]] || { echo "SSH key missing: $SSHKEY" >&2; exit 2; }
[[ -s "$SEED_TEMPLATE" ]] || { echo "regular ext4 seed template missing: $SEED_TEMPLATE" >&2; exit 2; }
for tool in python3 qemu-img mke2fs debugfs sha256sum ssh scp setsid awk tee stdbuf; do
    command -v "$tool" >/dev/null 2>&1 || { echo "required tool missing: $tool" >&2; exit 2; }
done

if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$PROJECT_DIR/results/feedback-ablation-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p -- "$OUTPUT_DIR" || exit 1
OUTPUT_DIR=$(cd -- "$OUTPUT_DIR" && pwd)
if [[ -n "$(find "$OUTPUT_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "refusing to overwrite non-empty output directory: $OUTPUT_DIR" >&2; exit 2
fi

PORTS=("$BASE_PORT" "$((BASE_PORT+1))" "$((BASE_PORT+2))" "$((BASE_PORT+3))")
if command -v ss >/dev/null 2>&1; then
    for port in "${PORTS[@]}"; do
        if ss -H -ltn "sport = :$port" | awk 'NF { found=1 } END { exit !found }'; then
            echo "SSH port already listening: $port" >&2; exit 1
        fi
    done
fi
python3 "$PROJECT_DIR/scripts/source_audit.py" --require-build || {
    echo "reviewed source or audited kernel build binding check failed" >&2; exit 2;
}

ROOTFS=$(cd -- "$(dirname -- "$ROOTFS")" && pwd)/$(basename -- "$ROOTFS")
ROOTFS_SHA256=$(sha256sum "$ROOTFS" | awk '{print $1}')
KERNEL_SHA256=$(sha256sum "$KERNEL" | awk '{print $1}')
SEED_DIR="$OUTPUT_DIR/seed"
SEED_IMAGE="$SEED_DIR/regular.raw"
SEED_PROGRAM="$SEED_DIR/upstream-ext4.syz"
SEED_METADATA="$SEED_DIR/metadata.json"
mkdir -p "$SEED_DIR"
echo "[setup] preparing one shared regular ext4 starting image"
python3 "$SEED_PREPARER" --seed "$SEED" --image "$SEED_IMAGE" \
    --template "$SEED_TEMPLATE" --program "$SEED_PROGRAM" --metadata "$SEED_METADATA" \
    > "$SEED_DIR/prepare.stdout" 2> "$SEED_DIR/prepare.stderr" || {
        echo "failed to prepare seed; see $SEED_DIR/prepare.stderr" >&2; exit 1;
    }
SEED_IMAGE_SHA256=$(sha256sum "$SEED_IMAGE" | awk '{print $1}')
printf 'seed=%s\nduration_seconds=%s\nsegments_cap=%s\nbatch_timeout_seconds=%s\nfail_fast=%s\nrootfs=%s\nrootfs_sha256=%s\nkernel=%s\nkernel_sha256=%s\nregular_seed_image=%s\nregular_seed_image_sha256=%s\ntarget_scope=%s\nfeedback_modes=%s\n' \
    "$SEED" "$DURATION_SECONDS" "$SEGMENTS" "$BATCH_TIMEOUT" "$FAIL_FAST" "$ROOTFS" "$ROOTFS_SHA256" \
    "$KERNEL" "$KERNEL_SHA256" "$SEED_IMAGE" "$SEED_IMAGE_SHA256" \
    "$TARGET_SCOPE" "${MODES[*]}" > "$OUTPUT_DIR/launch-metadata.txt"

declare -A ACTIVE_PIDS=() ARM_RC=() ARM_RUNS=()
stop_arms() {
    local arm pid runner_pid
    for arm in "${!ACTIVE_PIDS[@]}"; do
        pid=${ACTIVE_PIDS[$arm]}
        # Let the arm's logging pipeline remain alive while the campaign
        # runner handles SIGTERM and finishes writing its report. Terminating
        # the whole process group at once can close the pipe first.
        runner_pid=$(ps -eo pid=,pgid=,args= | awk -v pgid="$pid" \
            '$2 == pgid && index($0, "campaign_runner.py") { print $1; exit }')
        if [[ -n "$runner_pid" ]]; then
            kill -TERM "$runner_pid" 2>/dev/null || true
        else
            # The arm may still be in preflight or image preparation.
            kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        fi
    done
}
trap 'echo "[supervisor] stop requested; sending SIGTERM to all arms" >&2; stop_arms' INT TERM HUP

for i in "${!MODES[@]}"; do
    mode=${MODES[$i]}
    log="$OUTPUT_DIR/$mode.log"
    setsid bash "$ARM_RUNNER" "$mode" "${PORTS[$i]}" "$ROOTFS" "$SEED_IMAGE" \
        "$SEED" "$DURATION_SECONDS" "$log" "$RUNNER" "$TARGET_SCOPE" \
        "$SEGMENTS" "$TARGET_SYSCALLS" "$BATCH_TIMEOUT" &
    ACTIVE_PIDS[$mode]=$!
    echo "[$mode] started pid=${ACTIVE_PIDS[$mode]} port=${PORTS[$i]} log=$log"
done
echo "[supervisor] four arms running for ${DURATION_SECONDS}s; Ctrl-C requests graceful shutdown"
echo "[supervisor] live logs are also at $OUTPUT_DIR/{pc,pc_edge,pc_semantic,pc_edge_semantic}.log"

failure_seen=0
while ((${#ACTIVE_PIDS[@]})); do
    done_pid=""
    if wait -n -p done_pid "${ACTIVE_PIDS[@]}"; then wait_rc=0; else wait_rc=$?; fi
    [[ -n "$done_pid" ]] || continue
    done_arm=""
    for arm in "${!ACTIVE_PIDS[@]}"; do
        if [[ "${ACTIVE_PIDS[$arm]}" == "$done_pid" ]]; then done_arm=$arm; break; fi
    done
    [[ -n "$done_arm" ]] || continue
    ARM_RC[$done_arm]=$wait_rc
    unset 'ACTIVE_PIDS[$done_arm]'
    echo "[$done_arm] process exited rc=$wait_rc"
    if ((wait_rc != 0)); then
        if ((failure_seen == 0)); then
            failure_seen=1
            if ((FAIL_FAST)); then
                echo "[supervisor] arm failed; --fail-fast is set, stopping remaining arms" >&2
                stop_arms
            else
                echo "[supervisor] arm failed; remaining arms will continue; overall result will be incomplete" >&2
            fi
        fi
    fi
done
trap - INT TERM HUP

for mode in "${MODES[@]}"; do
    log="$OUTPUT_DIR/$mode.log"
    run_dir=$(grep -F "[$mode] run_dir=" "$log" 2>/dev/null | tail -n 1 | sed "s/^\\[$mode\\] run_dir=//")
    if [[ -n "$run_dir" && -d "$run_dir" ]]; then ARM_RUNS[$mode]=$run_dir; else ARM_RUNS[$mode]=""; fi
    printf '%s_exit_code=%s\n%s_run_dir=%s\n' "$mode" "${ARM_RC[$mode]:-125}" \
        "$mode" "${ARM_RUNS[$mode]}" >> "$OUTPUT_DIR/launch-metadata.txt"
done

summary_args=(--output "$OUTPUT_DIR/ablation.json" --seed "$SEED"
              --duration-seconds "$DURATION_SECONDS" --target-scope "$TARGET_SCOPE")
for mode in "${MODES[@]}"; do
    [[ -n "${ARM_RUNS[$mode]}" ]] && summary_args+=(--arm "$mode=${ARM_RUNS[$mode]}")
done
python3 "$SUMMARIZER" "${summary_args[@]}"
summary_rc=$?
printf 'summary_exit_code=%s\n' "$summary_rc" >> "$OUTPUT_DIR/launch-metadata.txt"
if ((failure_seen || summary_rc != 0)); then
    echo "[supervisor] incomplete/failed ablation; see $OUTPUT_DIR/ablation.json and arm logs" >&2
    exit 1
fi
echo "[supervisor] all arms passed; summary=$OUTPUT_DIR/ablation.md"
