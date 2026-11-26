#!/bin/sh
# All repeats share the same mount and program working directory.
set -eu
out=$1
program=$2
root=$3
run=$4
limit=$5
reference=$6
seed_device=$7
seed_inode=$8
trace=/sys/kernel/tracing
mkdir -p "$out/coverage"
echo 0 > "$trace/tracing_on"
echo 0 > "$trace/events/raw_syscalls/sys_enter/filter"
echo 0 > "$trace/events/raw_syscalls/sys_exit/filter"
echo > "$trace/trace"
cat /proc/sys/kernel/random/boot_id > "$out/boot-id.txt"
stat -c '%d' "$root" > "$out/target-device.txt"
dmesg > "$out/dmesg-before.txt"
stat -L -c '{"dev":%d,"ino":%i,"nlink":%h,"size":%s}' "$reference" > "$out/seed-before.json"

stable()
{
  for attempt in $(seq 1 30); do
    sh -c 'echo "CT_PHASE phase=BEGIN source=stable_snapshot target_pid=$$ id=snapshot-$$" > /sys/kernel/tracing/trace_marker; exec cat /sys/fs/ext4/vdb/counter_snapshot' > "$1" &
    observer=$!
    wait "$observer"
    echo "CT_PHASE phase=END source=stable_snapshot target_pid=$observer id=snapshot-$observer" > "$trace/trace_marker"
    if grep -q '"valid":true' "$1" && grep -q '"namespace_valid":true' "$1" && grep -q '"relations_valid":true' "$1"; then return 0; fi
    sleep 0.1
  done
  return 1
}

echo 1 > "$trace/tracing_on"
cat /sys/fs/ext4/vdb/counter_trace_state > "$out/target-start.json"
stable "$out/target-before.json" || echo before_unstable > "$out/snapshot-issue.txt"
set +e
CT_ROOT="$root" CT_PROGRAM_ID=p CT_CALL_PREFIX="r$run" CT_COVERAGE_DIR="$out/coverage" \
CT_KCOV_ENTRIES=1048576 CT_KCOV_FEATURE_SET=1 \
EFI_RUN_ID="$run" EFI_PLAN="$out/plan.txt" EFI_RESULT="$out/session.txt" \
EFI_SEED_REFERENCE="$reference" EFI_SEED_DEVICE="$seed_device" EFI_SEED_INODE="$seed_inode" \
  timeout -k 2 "$limit" "$program" > "$out/workload.txt" 2>&1
rc=$?
set -e
echo "$rc" > "$out/exit-code.txt"
stat -L -c '{"dev":%d,"ino":%i,"nlink":%h,"size":%s}' "$reference" > "$out/seed-after.json"
stable "$out/target-after.json" || echo after_unstable >> "$out/snapshot-issue.txt"
cat /sys/fs/ext4/vdb/counter_trace_state > "$out/target-end.json"
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
for cpu in "$trace"/per_cpu/cpu*; do
  cat "$cpu/stats" > "$out/$(basename "$cpu")-stats.txt"
done
dmesg > "$out/dmesg.txt"
echo "efi_round=$run exit=$rc"
