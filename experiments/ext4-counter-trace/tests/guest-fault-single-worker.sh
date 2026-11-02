#!/bin/sh
set -eu

trace=/sys/kernel/tracing
out=/tmp/fault-single
mkdir -p "$trace" "$out" /mnt/fault
mount -t tracefs tracefs "$trace" 2>/dev/null || true
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
echo "${EXT4_FAULT_TRACE_BUFFER_KB:-65536}" > "$trace/buffer_size_kb"
echo > "$trace/trace"
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
echo 1 > "$trace/events/raw_syscalls/sys_enter/enable"
echo 1 > "$trace/events/raw_syscalls/sys_exit/enable"
if [ -e "$trace/events/ext4_fault/ext4_fault_inject/enable" ]; then
    echo 1 > "$trace/events/ext4_fault/ext4_fault_inject/enable"
else
    echo 'missing ext4_fault_inject tracepoint' >&2
    exit 70
fi
mount -t ext4 /dev/vdb /mnt/fault
mkdir -p /mnt/fault/fixture
: > /mnt/fault/fixture/target
sync

stable_snapshot()
{
    path=$1
    candidate=$path.candidate
    attempt=0
    while [ "$attempt" -lt 100 ]; do
        attempt=$((attempt + 1))
        cat /sys/fs/ext4/vdb/counter_snapshot > "$path"
        cat /sys/fs/ext4/vdb/counter_snapshot > "$candidate"
        if grep -q '"valid":true' "$path" &&
           grep -q '"namespace_valid":true' "$path" &&
           grep -q '"relations_valid":true' "$path" &&
           grep -q '"valid":true' "$candidate" &&
           grep -q '"namespace_valid":true' "$candidate" &&
           grep -q '"relations_valid":true' "$candidate"; then
            left=$(sed -n 's/.*"free_clusters":\([0-9-]*\).*"free_inodes":\([0-9-]*\).*"dirs":\([0-9-]*\).*/\1 \2 \3/p' "$path")
            right=$(sed -n 's/.*"free_clusters":\([0-9-]*\).*"free_inodes":\([0-9-]*\).*"dirs":\([0-9-]*\).*/\1 \2 \3/p' "$candidate")
            if [ -n "$left" ] && [ "$left" = "$right" ]; then
                rm -f "$candidate"
                return 0
            fi
        fi
        sleep 0.05
    done
    rm -f "$candidate"
    return 1
}

stable_snapshot "$out/before.json"
echo 1 > "$trace/tracing_on"
CT_FAULT_PATH=/sys/fs/ext4/vdb/fault_inject \
CT_FAULT_STATUS=/sys/fs/ext4/vdb/fault_inject_status \
CT_FAULT_RESULT="$out/workload-result.json" \
CT_TRACE_MARKER="$trace/trace_marker" \
/tmp/fault-mballoc-single /mnt/fault > "$out/workload.txt" 2>&1
stable_snapshot "$out/after.json"

sync
umount /mnt/fault
mount -t ext4 /dev/vdb /mnt/fault
stable_snapshot "$out/remount.json"
/tmp/fault-mballoc-single --inspect /mnt/fault/fixture/target > "$out/remount-file-state.json"
umount /mnt/fault
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
for cpu in "$trace"/per_cpu/cpu*; do
    cat "$cpu/stats" > "$out/$(basename "$cpu")-stats.txt"
done
