#!/bin/sh
set -u

fault_id=
operation=
action=
fault_errno=-5
mount_options=
fixture=/tmp/fault-formula-single
out=/tmp/fault-formula
control=0
while [ "$#" -gt 0 ]; do
	case "$1" in
		--fault-id) fault_id=$2; shift 2 ;;
		--operation) operation=$2; shift 2 ;;
		--action) action=$2; shift 2 ;;
		--errno) fault_errno=$2; shift 2 ;;
		--mount-options) mount_options=$2; shift 2 ;;
		--fixture) fixture=$2; shift 2 ;;
		--evidence-dir) out=$2; shift 2 ;;
		--control) control=1; shift ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ -n "$fault_id" ] && [ -n "$operation" ] && [ -n "$action" ] || exit 2

trace=/sys/kernel/tracing
mnt=/mnt/fault
mkdir -p "$trace" "$out" "$mnt"
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

if [ -n "$mount_options" ]; then
	mount -t ext4 -o "$mount_options" /dev/vdb "$mnt" || exit 71
else
	mount -t ext4 /dev/vdb "$mnt" || exit 71
fi
mkdir -p "$mnt/fixture"
sync
echo 1 > "$trace/tracing_on"

set +e
CT_FAULT_PATH=/sys/fs/ext4/vdb/fault_inject \
CT_FAULT_STATUS=/sys/fs/ext4/vdb/fault_inject_status \
CT_FAULT_RESULT="$out/workload-result.json" \
CT_TRACE_MARKER="$trace/trace_marker" \
CT_FIXTURE_READY="$out/prepared" \
CT_FIXTURE_PROCEED="$out/proceed" \
CT_STABLE_BEFORE="$out/stable-before.json" \
CT_STABLE_ORACLE="$out/stable-oracle.json" \
CT_COUNTER_SNAPSHOT=/sys/fs/ext4/vdb/counter_snapshot \
	CT_FAULT_DISABLED="$control" \
	CT_FAULT_ERRNO="$fault_errno" \
	"$fixture" "$fault_id" "$operation" "$action" "$mnt" \
	> "$out/workload.txt" 2>&1 &
fixture_pid=$!
set -e
ready=0
for _ in $(seq 1 400); do
	if [ -e "$out/prepared" ]; then ready=1; break; fi
	if ! kill -0 "$fixture_pid" 2>/dev/null; then break; fi
	sleep 0.05
done
if [ "$ready" -ne 1 ]; then
	echo fixture_prepare_failed > "$out/run-status.txt"
	set +e; wait "$fixture_pid"; workload_rc=$?; set -e
	echo "workload_rc=$workload_rc" >> "$out/run-status.txt"
	exit 72
fi
# The fixture has completed all target setup (including unlink while its fd is
# open). It takes two healthy snapshots, then arms and closes the last fd.
: > "$out/proceed"
set +e
wait "$fixture_pid"
workload_rc=$?
set -e

snapshot()
{
	path=$1
	require_healthy=$2
	if "$fixture" --stable-snapshot "$path" "$require_healthy"; then
		return 0
	fi
	if [ ! -s "$path" ]; then
		echo '{"valid":false,"relations_valid":false,"observation_stable":false,"workers":1}' > "$path"
	fi
	return 1
}

after_rc=0
snapshot "$out/stable-after.json" 0 || after_rc=$?
sync
set +e
umount "$mnt"
unmount_rc=$?
set -e
if [ "$unmount_rc" -eq 0 ]; then
	echo unmount_ok > "$out/unmount-status.txt"
else
	printf 'unmount_failed:%s\n' "$unmount_rc" > "$out/unmount-status.txt"
fi
if [ "$unmount_rc" -eq 0 ]; then
	if [ -n "$mount_options" ]; then
		mount -t ext4 -o "$mount_options" /dev/vdb "$mnt"
	else
		mount -t ext4 /dev/vdb "$mnt"
	fi
	if [ "$?" -eq 0 ]; then
		remount_rc=0
		snapshot "$out/remount.json" 0 || remount_rc=$?
		if [ -e "$mnt/fixture/target" ]; then
			"$fixture" --inspect "$mnt/fixture/target" > "$out/remount-target.json" 2> "$out/remount-target.err" || :
		fi
		set +e; umount "$mnt"; remount_umount_rc=$?; set -e
	else
		remount_rc=1
		echo '{"valid":false,"relations_valid":false,"observation_stable":false,"workers":1}' > "$out/remount.json"
		remount_umount_rc=1
	fi
else
	remount_rc=1
	remount_umount_rc=1
	echo '{"valid":false,"relations_valid":false,"observation_stable":false,"workers":1}' > "$out/remount.json"
fi

echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
loss=0
stats_found=0
for cpu in "$trace"/per_cpu/cpu*; do
	[ -d "$cpu" ] || continue
	stats_found=1
	name=$(basename "$cpu")
	cat "$cpu/stats" > "$out/$name-stats.txt"
	drop=$(sed -n -e 's/^overrun: *\([0-9]*\).*/\1/p' \
	              -e 's/^commit overrun: *\([0-9]*\).*/\1/p' \
	              -e 's/^dropped events: *\([0-9]*\).*/\1/p' "$out/$name-stats.txt" |
		awk '{s+=$1} END {print s+0}')
	loss=$((loss + drop))
done
[ "$stats_found" -eq 1 ] || loss=1
printf 'workload_rc=%s\nafter_snapshot_rc=%s\nunmount_rc=%s\nremount_rc=%s\nremount_umount_rc=%s\ntrace_loss=%s\nworkers=1\n' \
	"$workload_rc" "$after_rc" "$unmount_rc" "$remount_rc" "$remount_umount_rc" "$loss" > "$out/run-status.txt"
exit "$workload_rc"
