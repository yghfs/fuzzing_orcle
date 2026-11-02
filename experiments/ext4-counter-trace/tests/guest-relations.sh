#!/bin/sh
set -eu
trace=/sys/kernel/tracing
out=/tmp/ct-relations
mkdir -p "$trace" "$out" /mnt/ct-relations
mount -t tracefs tracefs "$trace" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
echo 16384 > "$trace/buffer_size_kb"
echo > "$trace/trace"
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
echo 1 > "$trace/tracing_on"
dump_evidence()
{
    echo 0 > "$trace/tracing_on"
    cat "$trace/trace" > "$out/trace.txt"
    for cpu in "$trace"/per_cpu/cpu*; do
        cat "$cpu/stats" > "$out/$(basename "$cpu")-stats.txt"
    done
    dmesg > "$out/dmesg.txt"
}
trap dump_evidence EXIT
for pair in regular:vdb bigalloc:vdc inline:vdd legacy:vde; do
    kind=${pair%:*}; dev=${pair#*:}
    mount -o noatime "/dev/$dev" /mnt/ct-relations
    cat "/sys/fs/ext4/$dev/counter_trace_state" > "$out/$kind-start.json"
    cat "/sys/fs/ext4/$dev/counter_snapshot" > "$out/$kind-before.json"
    CT_SNAPSHOT_PATH="/sys/fs/ext4/$dev/counter_snapshot" CT_RESULT_PREFIX="$out/$kind" \
        /tmp/ct-relations-workload /mnt/ct-relations > "$out/$kind-workload.txt"
    cat "/sys/fs/ext4/$dev/counter_trace_state" > "$out/$kind-end.json"
    umount /mnt/ct-relations
done
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
for cpu in "$trace"/per_cpu/cpu*; do
    cat "$cpu/stats" > "$out/$(basename "$cpu")-stats.txt"
done
dmesg > "$out/dmesg.txt"
