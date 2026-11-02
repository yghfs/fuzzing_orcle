#!/bin/sh
set -eu
trace=/sys/kernel/tracing
out=/tmp/formula-probe
mkdir -p "$trace" "$out" /mnt/formula
mount -t tracefs tracefs "$trace" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
echo "${EXT4_FORMULA_TRACE_BUFFER_KB:-262144}" > "$trace/buffer_size_kb"
echo > "$trace/trace"
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
# Keep the buffer attributable to ext4 state transitions.  Raw syscall
# tracing multiplies the volume without adding formula evidence.
echo 0 > "$trace/events/raw_syscalls/sys_enter/enable" 2>/dev/null || true
echo 0 > "$trace/events/raw_syscalls/sys_exit/enable" 2>/dev/null || true
test ! -e /sys/module/ext4/parameters/ext4_fault_mode
mount -o noatime /dev/vdb /mnt/formula
for attempt in $(seq 1 100); do
	cat /sys/fs/ext4/vdb/counter_snapshot > "$out/before.json"
	if grep -q '"valid":true' "$out/before.json" &&
	   grep -q '"namespace_valid":true' "$out/before.json" && grep -q '"relations_valid":true' "$out/before.json"; then break; fi
	sleep 0.05
done
cat /sys/fs/ext4/vdb/counter_trace_state > "$out/start.json"
rm -f "$out/go"
EXT4_RESIZE_MEMORY_PRESSURE_MB=${EXT4_RESIZE_MEMORY_PRESSURE_MB:-0} \
	EXT4_RESIZE_ONLY=${EXT4_RESIZE_ONLY:-0} \
	EXT4_EA_ONLY=${EXT4_EA_ONLY:-0} \
	/tmp/formula-syscall-workload /mnt/formula > "$out/workload.txt" 2>&1 &
workload_pid=$!
echo "$workload_pid" > "$trace/set_event_pid"
echo 1 > "$trace/tracing_on"
touch "$out/go"
set +e
wait "$workload_pid"
rc=$?
set -e
echo workload_rc=$rc
cat "$out/workload.txt"
for attempt in $(seq 1 100); do
	cat /sys/fs/ext4/vdb/counter_snapshot > "$out/after.json"
	if grep -q '"valid":true' "$out/after.json" &&
	   grep -q '"namespace_valid":true' "$out/after.json" && grep -q '"relations_valid":true' "$out/after.json"; then break; fi
	sleep 0.05
done
cat /sys/fs/ext4/vdb/counter_trace_state > "$out/end.json"
sync
umount /mnt/formula
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
for cpu in "$trace"/per_cpu/cpu*; do
	cat "$cpu/stats" > "$out/$(basename "$cpu")-stats.txt"
done
dmesg > "$out/dmesg.txt"
