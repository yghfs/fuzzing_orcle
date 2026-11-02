#!/bin/sh
set -eu

trace=/sys/kernel/tracing
result=/var/lib/ext4-counter-trace
stage=${1:-}
mkdir -p "$trace" /mnt/fc "$result"

stable_snapshot()
{
	name=$1
	for attempt in $(seq 1 200); do
		cat /sys/fs/ext4/vdb/counter_snapshot > "$result/$name.json"
		if grep -q '"valid":true' "$result/$name.json" &&
		   grep -q '"relations_valid":true' "$result/$name.json" &&
		   grep -q '"namespace_valid":true' "$result/$name.json"; then
			return 0
		fi
		sleep 0.05
	done
	return 1
}

case "$stage" in
first)
	mount -t ext4 -o commit=600 /dev/vdb /mnt/fc
	nohup /tmp/ext4-fast-commit-inode-replay seed-inode-claim /mnt/fc/fc \
		> "$result/fc-seed.txt" 2>&1 < /dev/null &
	for attempt in $(seq 1 120); do
		grep -q '^fast_commit_inode_claim_ready ' "$result/fc-seed.txt" && break
		sleep 0.1
	done
	grep -q '^fast_commit_inode_claim_ready ' "$result/fc-seed.txt"
	cat /proc/fs/ext4/vdb/fc_info > "$result/fc-info-before-cut.txt"
	echo fast_commit_armed
	# The host hard-stops QEMU here, leaving the transaction for replay.
	;;

second)
	mount -t tracefs tracefs "$trace" 2>/dev/null || true
	echo 0 > "$trace/tracing_on"
	echo global > "$trace/trace_clock"
	for cpu in "$trace"/per_cpu/cpu*; do
		echo 32768 > "$cpu/buffer_size_kb"
	done
	: > "$trace/trace"
	for event in \
		events/ext4_counter/ext4_counter_event \
		events/ext4_fault/ext4_fault_prearm \
		events/ext4_fault/ext4_fault_inject \
		events/ext4/ext4_fc_replay_scan \
		events/ext4/ext4_fc_replay; do
		test -e "$trace/$event/enable" || {
			echo "missing trace event: $event" >&2
			exit 3
		}
		echo 1 > "$trace/$event/enable"
	done
	basename -a "$trace"/per_cpu/cpu* > "$result/cpus.txt"
	echo 1 > "$trace/tracing_on"

	# This process names and targets itself; ext4 replays the journal during
	# this mount syscall, after the mount option pre-arms the one-shot failure.
	/tmp/fault-fc-mount /dev/vdb /mnt/fc fast_commit_inode_claim \
		> "$result/fault-mount.json"
	cat /sys/fs/ext4/vdb/fault_inject_status > "$result/fault-status-hit.json"
	echo off > /sys/fs/ext4/vdb/fault_inject
	cat /sys/fs/ext4/vdb/fault_inject_status > "$result/fault-status-disabled.json"

	/tmp/ext4-fast-commit-inode-replay verify-inode-claim /mnt/fc/fc \
		> "$result/fc-verify.txt"
	stat -c '%i %h %s' /mnt/fc/fc/fresh > "$result/fresh-stat-after.txt"
	stable_snapshot stable-after-a
	stable_snapshot stable-after-b
	cat /sys/fs/ext4/vdb/counter_trace_state > "$result/counter-state-after.json"

	umount /mnt/fc
	mount -t ext4 /dev/vdb /mnt/fc
	/tmp/ext4-fast-commit-inode-replay verify-inode-claim /mnt/fc/fc \
		> "$result/fc-remount-verify.txt"
	stat -c '%i %h %s' /mnt/fc/fc/fresh > "$result/fresh-stat-remount.txt"
	stable_snapshot remount-a
	stable_snapshot remount-b
	umount /mnt/fc
	echo 0 > "$trace/tracing_on"
	cat "$trace/trace" > "$result/trace.txt"
	for cpu in "$trace"/per_cpu/cpu*; do
		cat "$cpu/stats" > "$result/$(basename "$cpu")-stats.txt"
	done
	dmesg > "$result/dmesg.txt"
	touch "$result/guest-done"
	;;

*)
	echo "usage: $0 first|second" >&2
	exit 2
	;;
esac
