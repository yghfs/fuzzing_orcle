#!/bin/sh
# One boot = one case. Phase A (no plan) is the no-fault control and also
# discovers the stack-context hashes; phase B replays the same case with a plan.
set -eu
out=$1
mode=$2
mount=$3
shift 3

trace=/sys/kernel/tracing
mkdir -p "$out" "$mount"
echo 0 > "$trace/tracing_on"
echo > "$trace/trace"
echo 1 > "$trace/events/ext4_efi/efi_site/enable"
echo 1 > "$trace/events/ext4_efi/efi_formula/enable"
echo 1 > "$trace/tracing_on"

# Poll the stable counter snapshot: the checker's own self-consistency gate.
stable()
{
	for attempt in $(seq 1 60); do
		cat /sys/fs/ext4/vdb/counter_snapshot > "$1" 2>/dev/null || true
		if grep -q '"valid":true' "$1" 2>/dev/null &&
		   grep -q '"namespace_valid":true' "$1" &&
		   grep -q '"relations_valid":true' "$1"; then
			return 0
		fi
		sleep 0.1
	done
	return 1
}

cat /sys/fs/ext4/vdb/counter_trace_state > "$out/state-start.json" 2>/dev/null || true
stable "$out/snapshot-before.json" || echo before_unstable > "$out/snapshot-note.txt"

set +e
CT_ROOT="$mount" EFI_RUN_ID=1 EFI_PLAN="$CT_PLAN" EFI_RESULT="$out/session.txt" \
	"$CT_FIXTURE" "$mode" "$mount" "$@" > "$out/workload.txt" 2>&1
echo $? > "$out/exit-code.txt"
set -e

stable "$out/snapshot-after.json" || echo after_unstable >> "$out/snapshot-note.txt"
cat /sys/fs/ext4/vdb/counter_trace_state > "$out/state-end.json" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
cat "$trace/trace" > "$out/trace.txt"
dmesg > "$out/dmesg.txt"
echo "repro_done mode=$mode"
