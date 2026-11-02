#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EXTRUNC_MLOCK_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
config="${EXT4_EXTRUNC_MLOCK_CONFIG:-${work_dir}/kernels/ext4-count-normal-new/config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_EXTRUNC_MLOCK_PORT:-12751}"
memory_mb="${EXT4_EXTRUNC_MLOCK_MEMORY_MB:-180}"
attempts="${EXT4_EXTRUNC_MLOCK_ATTEMPTS:-128}"
run_dir="${EXT4_EXTRUNC_MLOCK_RUN_DIR:-${work_dir}/ext4-extent-truncate-enomem-$(date +%Y%m%d%H%M%S)-$$}"
mkdir -p "${run_dir}"
grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${config}"
if grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${config}"; then
	echo 'fault injection enabled in discovery kernel' >&2
	exit 2
fi
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-extent-truncate-enomem.c"
qemu-img create -q -f raw "${run_dir}/test.raw" 256M
mke2fs -q -F -t ext4 -b 4096 -N 8192 -O '^orphan_file' \
	-J size=4 -E lazy_itable_init=0 "${run_dir}/test.raw"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 256 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qpid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qpid}" 2>/dev/null || true; }
trap cleanup EXIT
opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4 \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/extrunc; mountpoint -q /mnt/extrunc || mount /dev/vdb /mnt/extrunc; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/workload" root@127.0.0.1:/tmp/extrunc
ssh "${opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
set +e
ssh "${opts[@]}" root@127.0.0.1 \
	"/tmp/extrunc /mnt/extrunc '${memory_mb}' '${attempts}'; rc=\$?; echo workload_rc=\$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'EXT4 invariant violation|error_exit_inode|extent_blocks|Out of memory|Killed process|BUG:' | tail -200; sync; umount /mnt/extrunc; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do
	if ! kill -0 "${qpid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
qemu-img convert -q -f raw -O raw "${run_dir}/test.raw" "${run_dir}/recovered.raw"
set +e
e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' "${run_dir}" "${runtime_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'attempt=|workload_rc|model_inode_bitmap|model_extent_blocks|error_exit_inode|violations_total|snapshot OK|Block bitmap differences|Free .* count|Inode bitmap differences|EXT4 invariant violation' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" || true
