#!/usr/bin/env bash
set -uo pipefail

if (($# != 12)); then
    echo "usage: run-feedback-ablation-arm.sh MODE PORT ROOTFS SEED_IMAGE SEED DURATION OUTPUT_LOG RUNNER TARGET_SCOPE SEGMENTS TARGET_SYSCALLS BATCH_TIMEOUT" >&2
    exit 2
fi
mode=$1
port=$2
rootfs=$3
seed_image=$4
seed=$5
duration=$6
log=$7
runner=$8
target_scope=$9
segments=${10}
target_syscalls=${11}
batch_timeout=${12}

case "$mode" in pc|pc_edge|pc_semantic|pc_edge_semantic) ;; *) exit 2 ;; esac
[[ "$batch_timeout" =~ ^[0-9]+$ ]] && ((batch_timeout >= 1 && batch_timeout <= 300)) || {
    echo "batch timeout must be an integer in [1,300]" >&2
    exit 2
}

stdbuf -oL -eL bash "$runner" \
    --mode single --segments "$segments" --duration-seconds "$duration" \
    --target-syscalls "$target_syscalls" \
    --segment-seconds 1 --max-batches 1 --batch-timeout "$batch_timeout" \
    --cpus 1 --guest-memory-mb 1024 --workers 1 --memory-mb 0 \
    --syzkaller-calls 16 --resource-valid-every 1 --fresh-generate-every 0 \
    --resource-seed-refresh-every 4 --target-scope "$target_scope" \
    --target-kind regular --scheduler-policy rarity_fair --feedback-mode "$mode" \
    --strict-kcov-feedback --port "$port" --seed "$seed" \
    --rootfs-backing "$rootfs" --comparison-seed-image "$seed_image" \
    2>&1 | awk -v prefix="[$mode]" '{ print prefix " " $0; fflush(); }' | tee -p "$log"

pipeline_status=("${PIPESTATUS[@]}")
exit "${pipeline_status[0]}"
