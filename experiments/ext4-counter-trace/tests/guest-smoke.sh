#!/bin/sh
set -eu
trace=/sys/kernel/tracing
mkdir -p "$trace" /mnt/ct /tmp/ct-results
mount -t tracefs tracefs "$trace" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
echo "${EXT4_CT_TRACE_BUFFER_KB:-131072}" > "$trace/buffer_size_kb"
echo > "$trace/trace"
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
echo 1 > "$trace/tracing_on"
test ! -e /sys/module/ext4/parameters/ext4_fault_mode
uname -r > /tmp/ct-results/kernel.txt

stable()
{
	device=$1
	output=$2
	for attempt in $(seq 1 100); do
		cat "/sys/fs/ext4/$device/counter_snapshot" > "$output"
		if grep -q '"valid":true' "$output"; then
			return 0
		fi
		sleep 0.05
	done
	cat "$output"
	return 1
}

for pair in regular:vdb bigalloc:vdc nojournal:vdd lazy:vde; do
	kind=${pair%:*}
	dev=${pair#*:}
	mount "/dev/$dev" /mnt/ct
	cat "/sys/fs/ext4/$dev/counter_trace_state" > "/tmp/ct-results/$kind-start.json"
	stable "$dev" "/tmp/ct-results/$kind-before.json"
	/tmp/ct-workload /mnt/ct/run > "/tmp/ct-results/$kind-workload.txt" &
	worker=$!
	for n in $(seq 1 30); do
		cat "/sys/fs/ext4/$dev/counter_snapshot" >> "/tmp/ct-results/$kind-concurrent.jsonl"
	done
	wait "$worker"
	if [ "$kind" = regular ]; then
		/tmp/buddy-stress /mnt/ct/buddy > "/tmp/ct-results/buddy-stress.txt"
	fi
	# Deliberately sample before sync or unmount.
	stable "$dev" "/tmp/ct-results/$kind-after.json"
	mkdir /mnt/ct/retained
	dd if=/dev/zero of=/mnt/ct/retained/data bs=4096 count=256 conv=fsync 2>/dev/null
	stable "$dev" "/tmp/ct-results/$kind-retained.json"
	rm /mnt/ct/retained/data
	rmdir /mnt/ct/retained
	stable "$dev" "/tmp/ct-results/$kind-released.json"
	if fallocate -l 1G /mnt/ct/enospc > "/tmp/ct-results/$kind-enospc.txt" 2>&1; then
		echo "Expected ENOSPC" >&2
		exit 1
	fi
	grep -q 'No space left on device' "/tmp/ct-results/$kind-enospc.txt"
	rm /mnt/ct/enospc
	mkdir /mnt/ct/ns
	printf payload > /mnt/ct/ns/a
	ln /mnt/ct/ns/a /mnt/ct/ns/b
	mv /mnt/ct/ns/a /mnt/ct/ns/c
	ln -s c /mnt/ct/ns/s
	rm /mnt/ct/ns/b /mnt/ct/ns/c /mnt/ct/ns/s
	rmdir /mnt/ct/ns
	stable "$dev" "/tmp/ct-results/$kind-namespace.json"
	cat "/sys/fs/ext4/$dev/counter_trace_state" > "/tmp/ct-results/$kind-end.json"
	umount /mnt/ct
done
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > /tmp/ct-results/trace.txt
for cpu in "$trace"/per_cpu/cpu*; do
	cat "$cpu/stats" > "/tmp/ct-results/$(basename "$cpu")-stats.txt"
done
dmesg > /tmp/ct-results/dmesg.txt
echo counter_trace_smoke=complete
