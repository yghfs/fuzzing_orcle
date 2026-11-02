#!/bin/sh
set -u

fixture=/tmp/natural-rename-stress
out=/tmp/fault-formula
mount_options=
while [ "$#" -gt 0 ]; do
	case "$1" in
		--fixture) fixture=$2; shift 2 ;;
		--evidence-dir) out=$2; shift 2 ;;
		--mount-options) mount_options=$2; shift 2 ;;
		*) shift ;;
	esac
done

trace=/sys/kernel/tracing
mnt=/mnt/fault
mkdir -p "$trace" "$out" "$mnt"
mount -t tracefs tracefs "$trace" 2>/dev/null || true
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
echo "${EXT4_FAULT_TRACE_BUFFER_KB:-32768}" > "$trace/buffer_size_kb"
echo > "$trace/trace"
for event in jbd2_handle_start jbd2_handle_stats jbd2_handle_extend jbd2_handle_restart; do
	if [ -e "$trace/events/jbd2/$event/enable" ]; then
		echo 1 > "$trace/events/jbd2/$event/enable"
	fi
done
if [ -n "$mount_options" ]; then
	mount -t ext4 -o "$mount_options" /dev/vdb "$mnt" || exit 71
else
	mount -t ext4 /dev/vdb "$mnt" || exit 71
fi
mkdir -p "$mnt/fixture"
sync
echo 1 > "$trace/tracing_on"

set +e
CT_FAULT_RESULT="$out/workload-result.json" \
CT_TRACE_MARKER="$trace/trace_marker" \
CT_FIXTURE_READY="$out/prepared" \
CT_FIXTURE_PROCEED="$out/proceed" \
CT_STABLE_BEFORE="$out/stable-before.json" \
CT_STABLE_ORACLE="$out/stable-oracle.json" \
	NATURAL_RENAME_SECONDS="${NATURAL_RENAME_SECONDS:-20}" \
	NATURAL_RENAME_WORKERS="${NATURAL_RENAME_WORKERS:-4}" \
	NATURAL_RENAME_FILLERS="${NATURAL_RENAME_FILLERS:-640}" \
	"$fixture" natural-rename-stress natural_rename_stress natural "$mnt" \
	> "$out/workload.txt" 2>&1 &
fixture_pid=$!
set -e

ready=0
for _ in $(seq 1 600); do
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
: > "$out/proceed"
set +e
wait "$fixture_pid"
workload_rc=$?
set -e

snapshot()
{
	path=$1
	if "$fixture" --stable-snapshot "$path" 0; then return 0; fi
	echo '{"valid":false,"relations_valid":false,"namespace_valid":false,"observation_stable":false,"trace_loss":1,"workers":4}' > "$path"
	return 1
}

after_rc=0
snapshot "$out/stable-after.json" || after_rc=$?
sync
set +e
umount "$mnt"
unmount_rc=$?
set -e
if [ "$unmount_rc" -eq 0 ]; then
	if [ -n "$mount_options" ]; then
		mount -t ext4 -o "$mount_options" /dev/vdb "$mnt"
	else
		mount -t ext4 /dev/vdb "$mnt"
	fi
	if [ "$?" -eq 0 ]; then
		remount_rc=0
		snapshot "$out/remount.json" || remount_rc=$?
		set +e; umount "$mnt"; remount_umount_rc=$?; set -e
	else
		remount_rc=1
		remount_umount_rc=1
		echo '{"valid":false,"relations_valid":false,"namespace_valid":false,"observation_stable":false,"trace_loss":1,"workers":4}' > "$out/remount.json"
	fi
else
	remount_rc=1
	remount_umount_rc=1
	echo '{"valid":false,"relations_valid":false,"namespace_valid":false,"observation_stable":false,"trace_loss":1,"workers":4}' > "$out/remount.json"
fi

echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
loss=0
for cpu in "$trace"/per_cpu/cpu*; do
	[ -d "$cpu" ] || continue
	name=$(basename "$cpu")
	cat "$cpu/stats" > "$out/$name-stats.txt"
	drop=$(sed -n -e 's/^overrun: *\([0-9]*\).*/\1/p' \
	              -e 's/^commit overrun: *\([0-9]*\).*/\1/p' \
	              -e 's/^dropped events: *\([0-9]*\).*/\1/p' "$out/$name-stats.txt" |
		awk '{s+=$1} END {print s+0}')
	loss=$((loss + drop))
done
printf 'workload_rc=%s\nafter_snapshot_rc=%s\nunmount_rc=%s\nremount_rc=%s\nremount_umount_rc=%s\ntrace_loss=%s\nworkers=%s\n' \
	"$workload_rc" "$after_rc" "$unmount_rc" "$remount_rc" \
	"$remount_umount_rc" "$loss" "${NATURAL_RENAME_WORKERS:-4}" > "$out/run-status.txt"
exit "$workload_rc"
