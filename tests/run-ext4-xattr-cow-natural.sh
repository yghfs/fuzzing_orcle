#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${XATTR_COW_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
config="${XATTR_COW_CONFIG:-${work_dir}/kernels/ext4-count-normal-new/config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
run_dir="${XATTR_COW_RUN_DIR:-${work_dir}/ext4-xattr-cow-natural-$(date +%Y%m%d%H%M%S)-$$}"
port="${XATTR_COW_PORT:-12183}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${config}"
grep -qx '# CONFIG_EXT4_FAULT_INJECTION is not set' "${config}"
test -r "${kernel}"
test -r "${root_base}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-xattr-cow-natural.c"
qemu-img create -q -f raw "${run_dir}/test.raw" 128M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 \
	-m 0 -J size=4 -E lazy_itable_init=0 "${run_dir}/test.raw"
e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${run_dir}/test.raw" "${run_dir}/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024M -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/xattr-cow; mountpoint -q /mnt/xattr-cow || mount /dev/vdb /mnt/xattr-cow; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		mounted=1
		break
	fi
	sleep 1
done
if [[ "${mounted}" != 1 ]]; then
	echo "guest did not mount checker filesystem: ${run_dir}" >&2
	exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; mount -o remount,commit=1 /mnt/xattr-cow; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
scp -q -i "${ssh_key}" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/workload" root@127.0.0.1:/tmp/xattr-cow-workload
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'/tmp/xattr-cow-workload /mnt/xattr-cow/workload; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | grep -E "xattr_block_refs|EXT4 invariant violation|EXT4-fs.*error|BUG:|WARNING:" | tail -200 || true; sync; umount /mnt/xattr-cow; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e

for _ in $(seq 1 90); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo 'guest did not finish clean poweroff; offline checking refused' \
		>"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" \
		"${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
{
	printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' \
		"${run_dir}" "${runtime_rc}" "${fsck_rc}"
	rg -n 'xattr_cow_natural|workload_rc|model_xattr_block_refs|xattr_block_refs=|violations_total|snapshot OK|EXT4 invariant violation' \
		"${run_dir}/runtime.txt" || true
	tail -20 "${run_dir}/fsck.txt"
} | tee "${run_dir}/report.txt"
test "${runtime_rc}" -eq 0
test "${fsck_rc}" -eq 0
