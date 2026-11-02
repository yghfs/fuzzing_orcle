#!/bin/sh
set -eu
trace=/sys/kernel/tracing
out=/tmp/ct-results
mkdir -p "$trace" /mnt/ct "$out"
mount -t tracefs tracefs "$trace" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
echo 65536 > "$trace/buffer_size_kb"
echo > "$trace/trace"
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
echo 1 > "$trace/events/raw_syscalls/sys_enter/enable"
echo 1 > "$trace/events/raw_syscalls/sys_exit/enable"
echo 1 > "$trace/tracing_on"
test ! -e /sys/module/ext4/parameters/ext4_fault_mode
uname -r > "$out/kernel.txt"
cat /proc/sys/kernel/random/boot_id > "$out/boot-id.txt"
stable()
{
    dev=$1; file=$2
    for attempt in $(seq 1 100); do
        cat "/sys/fs/ext4/$dev/counter_snapshot" > "$file"
        if grep -q '"valid":true' "$file" && grep -q '"namespace_valid":true' "$file" && grep -q '"relations_valid":true' "$file"; then return; fi
        sleep 0.05
    done
    return 1
}
for pair in regular:vdb bigalloc:vdc inline:vdd; do
    kind=${pair%:*}; dev=${pair#*:}
    mount -o noatime "/dev/$dev" /mnt/ct
    for mode in single concurrent; do
        name=$kind-$mode
        cat "/sys/fs/ext4/$dev/counter_trace_state" > "$out/$name-start.json"
        stable "$dev" "$out/$name-before.json"
        unset CT_EXPECT_INLINE
        if [ "$kind" = inline ]; then export CT_EXPECT_INLINE=1; fi
        unset CT_BIGALLOC
        if [ "$kind" = bigalloc ]; then export CT_BIGALLOC=1; fi
        CT_SNAPSHOT_PATH="/sys/fs/ext4/$dev/counter_snapshot" CT_RESULT_PREFIX="$out/$name" /tmp/ct-chain-workload /mnt/ct "$mode" > "$out/$name-workload.txt"
        # Check before forcing durability; then after syncfs/unmount compare disk.
        stable "$dev" "$out/$name-after.json"
        cat "/sys/fs/ext4/$dev/counter_trace_state" > "$out/$name-end.json"
    done
    sync
    umount /mnt/ct
done
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
for cpu in "$trace"/per_cpu/cpu*; do cat "$cpu/stats" > "$out/$(basename "$cpu")-stats.txt"; done
dmesg > "$out/dmesg.txt"
echo counter_trace_chain=complete
