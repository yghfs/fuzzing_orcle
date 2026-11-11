#!/bin/busybox sh

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /sys/kernel/debug /sys/kernel/tracing /mnt /tmp/coverage
mount -t debugfs debugfs /sys/kernel/debug
mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true

mount -t ext4 -o errors=continue,noatime /dev/vda /mnt || {
	echo 'MOUNT_FAILED'
	exec /bin/busybox poweroff -f
}

trace=/sys/kernel/tracing
echo 16384 > "$trace/buffer_size_kb" 2>/dev/null || true
echo 0 > "$trace/tracing_on"
echo > "$trace/trace"
if [ -e "$trace/events/ftrace/print/enable" ]; then
	echo 1 > "$trace/events/ftrace/print/enable"
fi
echo 1 > "$trace/tracing_on"

echo 'REALIO_REPLAY_READY'
read -r answer </dev/console
cd /mnt/syz-work || exec /bin/busybox poweroff -f
export CT_ROOT=/mnt/syz-work
export CT_PROGRAM_ID=realio
export CT_CALL_PREFIX=realio
export CT_COVERAGE_DIR=/tmp/coverage
export CT_KCOV_ENTRIES=65536
echo 'REALIO_WORKLOAD_START'
/bin/replay-workload 2>&1
workload_rc=$?
echo "REALIO_WORKLOAD_EXIT rc=$workload_rc"
echo 0 > "$trace/tracing_on"
echo 'REALIO_TRACE_BEGIN'
cat "$trace/trace"
echo 'REALIO_TRACE_END'
sync
echo 'REALIO_PRE_UNMOUNT'
cd /
umount /mnt
echo "REALIO_UNMOUNT_EXIT rc=$?"
exec /bin/busybox poweroff -f
