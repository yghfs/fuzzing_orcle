#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EVICT_PRESSURE_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
config="${EXT4_EVICT_PRESSURE_CONFIG:-${work_dir}/kernels/ext4-count-normal-new/config}"
root_base="${EXT4_EVICT_PRESSURE_ROOT:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_EVICT_PRESSURE_PORT:-12461}"
qemu_mem="${EXT4_EVICT_PRESSURE_QEMU_MEM:-512}"
pressure_mb="${EXT4_EVICT_PRESSURE_MB:-220}"
rounds="${EXT4_EVICT_PRESSURE_ROUNDS:-8}"
orphans="${EXT4_EVICT_PRESSURE_ORPHANS:-64}"
run_dir="${EXT4_EVICT_PRESSURE_RUN_DIR:-${work_dir}/ext4-evict-memory-pressure-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${config}"
if grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${config}"; then
	echo 'fault injection enabled in discovery kernel' >&2
	exit 2
fi
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-evict-memory-pressure.c"
qemu-img create -q -f raw "${run_dir}/test.raw" 1G
mke2fs -q -F -t ext4 -O orphan_file,^64bit -b 4096 -N 65536 \
	-J size=4 -E lazy_itable_init=0 "${run_dir}/test.raw"
e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${qemu_mem}" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 150); do
	if ssh "${opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-evict-pressure; mountpoint -q /mnt/ext4-evict-pressure || mount /dev/vdb /mnt/ext4-evict-pressure; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/workload" \
	root@127.0.0.1:/tmp/ext4-evict-memory-pressure
set +e
ssh "${opts[@]}" root@127.0.0.1 \
	"timeout --foreground 180s /tmp/ext4-evict-memory-pressure /mnt/ext4-evict-pressure ${rounds} ${orphans} ${pressure_mb}; rc=\$?; echo workload_rc=\${rc}; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'EXT4 invariant violation|Out of memory|Killed process|WARNING:|BUG:' | tail -200; sync; umount /mnt/ext4-evict-pressure; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	set +e
	e2fsck -E journal_only -y "${run_dir}/test.raw" >"${run_dir}/journal.txt" 2>&1
	journal_rc=$?
	e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s pressure_mb=%s qemu_mem=%s\n' \
	"${run_dir}" "${runtime_rc}" "${fsck_rc}" "${pressure_mb}" "${qemu_mem}" \
	| tee "${run_dir}/report.txt"
rg -n 'round=|evict_pressure|workload_rc|model_(inode_bitmap|orphan_file_entries)|error_exit_inode_free|violations_total|last_violation|snapshot|Out of memory|Killed process|Inode bitmap differences|Orphan file' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
