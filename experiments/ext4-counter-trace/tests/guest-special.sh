#!/bin/sh
# Targeted verification for the special counter paths that the ordinary smoke
# does not reach: bigalloc rereserve, online resize, fast-commit replay and
# bitmap-corruption exclusion.
#
# The runner invokes this script twice.  "first" runs the workloads that need
# a mounted filesystem and leaves the fast-commit image dirty, then exits
# without unmounting it; the host cuts power there.  "second" runs with the
# fast-commit journal already unclean, so mounting vdd performs the replay
# while tracing is enabled, and then exercises the corrupted bitmaps.
set -eu
trace=/sys/kernel/tracing
result=/var/lib/ext4-counter-trace
stage=${1:-}
fc_mode=${FC_MODE:-dir}
mkdir -p "$trace" /mnt/spec "$result"
# Each guest boot exports only its own trace.  The host already saves the
# first-boot evidence before cutting power, so discard its guest-side copy
# before recording the second boot.
if [ "$stage" = second ]; then
	: > "$result/trace.txt"
	rm -rf "$result/stages"
fi
mount -t tracefs tracefs "$trace" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo global > "$trace/trace_clock"
for cpu in "$trace"/per_cpu/cpu*; do
	echo 32768 > "$cpu/buffer_size_kb"
done
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
if [ -e "$trace/events/ext4/ext4_fc_replay/enable" ]; then
	echo 1 > "$trace/events/ext4/ext4_fc_replay/enable"
fi

start_stage()
{
	stage_tag=$1
	stage_dev=$2
	stage_dir="$result/stages/$stage_tag"
	mkdir -p "$stage_dir"
	cat /sys/class/block/$stage_dev/dev > "$stage_dir/dev.txt"
	basename -a "$trace"/per_cpu/cpu* > "$stage_dir/cpus.txt"
	echo 0 > "$trace/tracing_on"
	echo > "$trace/trace"
	echo 1 > "$trace/tracing_on"
}

end_stage()
{
	cat /sys/fs/ext4/$stage_dev/counter_trace_state > "$stage_dir/end.json"
	echo 0 > "$trace/tracing_on"
	cat "$trace/trace" > "$stage_dir/trace.txt"
	cat "$stage_dir/trace.txt" >> "$result/trace.txt"
	for cpu in "$trace"/per_cpu/cpu*; do
		cat "$cpu/stats" > "$stage_dir/$(basename "$cpu")-stats.txt"
	done
}

stable()
{
	for attempt in $(seq 1 200); do
		cat /sys/fs/ext4/$1/counter_snapshot > "$result/$2.json"
		grep -q '"namespace_valid":true' "$result/$2.json" && grep -q '"relations_valid":true' "$result/$2.json" && return 0
		sleep 0.05
	done
	return 1
}

fsync_path()
{
	# The minimal rootfs does not ship an fsync(1) utility.  Use Perl's
	# POSIX wrapper so the workload issues an actual fsync(2) for the file
	# or directory that carries the fast-commit operation.
	perl -MIO::Handle -e '
		my ($path) = @ARGV;
		open(my $fh, -d $path ? "<" : "+<", $path) or die "open $path: $!";
		$fh->sync() or die "fsync $path: $!";
		close($fh) or die "close $path: $!";
	' "$1"
}

write_fsync()
{
	# Write a deterministic block and flush the file.  A second write after a
	# completed baseline transaction is what makes ext4 emit ADD_RANGE/INODE
	# fast-commit records instead of folding the operation into the first full
	# journal commit.
	perl -MIO::Handle -e '
		my ($path, $byte) = @ARGV;
		open(my $fh, "+<", $path) or die "open $path: $!";
		seek($fh, 0, 0) or die "seek $path: $!";
		my $buf = $byte x 4096;
		syswrite($fh, $buf) == 4096 or die "write $path: $!";
		$fh->sync() or die "fsync $path: $!";
		close($fh) or die "close $path: $!";
	' "$1" "$2"
}

case "$stage" in
first)
	############################ bigalloc rereserve #########################
	start_stage bigalloc vdb
	mount -t ext4 /dev/vdb /mnt/spec
	mkdir -p /mnt/spec/rr
	# Keep the delayed allocation reservation live while removing a partial
	# cluster.  The punch must precede writeback; otherwise the reservation
	# has already become a normal extent and RERESERVE is not taken.
	dd if=/dev/zero of=/mnt/spec/rr/f bs=4096 count=16 2>/dev/null
	fallocate -p -o 4096 -l 4096 /mnt/spec/rr/f 2>/dev/null || true
	fallocate -d -o 4096 -l 4096 /mnt/spec/rr/f 2>/dev/null || true
	truncate -s 65536 /mnt/spec/rr/f
	rm -f /mnt/spec/rr/f
	rmdir /mnt/spec/rr
	stable vdb bigalloc-special
	end_stage
	umount /mnt/spec
	echo first_stage_bigalloc=done >> "$result/stages.txt"

	############################ online resize ################################
	start_stage resize vdc
	mount -t ext4 /dev/vdc /mnt/spec
	stable vdc special-resize-before
	resize2fs /dev/vdc > "$result/resize.txt" 2>&1
	sync
	stable vdc special-resize-after
	cat /sys/fs/ext4/vdc/counter_trace_state > "$result/resize-state.json"
	end_stage
	umount /mnt/spec
	echo first_stage_resize=done >> "$result/stages.txt"

	############################ fast commit arm ##############################
	# A long commit interval keeps the transaction in the journal when power is
	# cut, so the next mount has to replay the fast commit instead of finding a
	# clean journal.  Only metadata operations are queued: data=ordered would
	# force a data writeback and a full journal commit before the power cut.
	mount -t ext4 -o commit=600 /dev/vdd /mnt/spec
	# Complete a baseline, then publish the selected ordinary namespace operation
	# in a later fast-commit transaction before the host cuts power.
	if [ "$fc_mode" = rmdir ]; then
		fc_seed_mode=seed-rmdir
	elif [ "$fc_mode" = link ]; then
		fc_seed_mode=seed-link
	else
		fc_seed_mode=seed
	fi
	nohup /tmp/ext4-fast-commit-inode-replay "$fc_seed_mode" /mnt/spec/fc \
		> "$result/fc-seed.txt" 2>&1 < /dev/null &
	for attempt in $(seq 1 120); do
		grep -q fast_commit_ready "$result/fc-seed.txt" && break
		sleep 0.1
	done
	grep -q fast_commit_ready "$result/fc-seed.txt"
	cat /proc/fs/ext4/vdd/fc_info > "$result/fc-info-before-cut.txt" 2>/dev/null || true
	echo fast_commit_prepare=dirty >> "$result/stages.txt"
	# Each fsync above publishes a fast-commit tail.  Keep the filesystem
	# mounted and cut power immediately, so the next mount must replay it.
	echo fast_commit_armed
	# Intentionally leave /dev/vdd mounted; the host cuts power now.
	;;

second)
	############################ fast commit replay ###########################
	# Tracing is enabled before the mount, so the replay performed by the mount
	# itself is recorded.
	start_stage replay vdd
	mount -t ext4 /dev/vdd /mnt/spec
	if [ "$fc_mode" = rmdir ]; then
		fc_verify_mode=verify-rmdir
	elif [ "$fc_mode" = link ]; then
		fc_verify_mode=verify-link
	else
		fc_verify_mode=verify
	fi
	/tmp/ext4-fast-commit-inode-replay "$fc_verify_mode" /mnt/spec/fc > "$result/fc-verify.txt"
	sync
	stable vdd special-replay-after
	end_stage
	umount /mnt/spec

	############################ corruption exclusion #######################
	start_stage corrupt vde
	mount -t ext4 -o errors=continue /dev/vde /mnt/spec 2> "$result/corrupt-mount.err" || true
	mkdir -p /mnt/spec/force-read 2>/dev/null || true
	fallocate -l 8M /mnt/spec/force-read/x 2>/dev/null || true
	cat /sys/fs/ext4/vde/counter_trace_state > "$result/corrupt-before.json" 2>/dev/null || true
	end_stage
	umount /mnt/spec 2>/dev/null || true

	for cpu in "$trace"/per_cpu/cpu*; do
		cat "$cpu/stats" > "$result/$(basename "$cpu")-stats.txt"
	done
	dmesg > "$result/dmesg.txt"
	touch "$result/special-done"
	;;

*)
	echo "usage: guest-special.sh first|second" >&2
	exit 2
	;;
esac
