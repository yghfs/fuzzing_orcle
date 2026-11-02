#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EVICT_NATURAL_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
config="${EXT4_EVICT_NATURAL_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_EVICT_NATURAL_ROOT:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_EVICT_NATURAL_PORT:-11968}"
run_dir="${EXT4_EVICT_NATURAL_RUN_DIR:-${work_dir}/ext4-evict-remount-natural-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${config}"
grep -q '^# CONFIG_EXT4_FAULT_INJECTION is not set$' "${config}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-evict-remount-natural.c"
qemu-img create -q -f raw "${run_dir}/test.raw" 256M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 \
	-J size=4 -E lazy_itable_init=0 "${run_dir}/test.raw"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768M -smp 1 \
	-kernel "${kernel}" -drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw noapic console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qpid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qpid}" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-evict-natural; mountpoint -q /mnt/ext4-evict-natural || mount /dev/vdb /mnt/ext4-evict-natural; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then break; fi
	sleep 1
done
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/workload" \
	root@127.0.0.1:/tmp/ext4-evict-remount-natural
ssh "${opts[@]}" root@127.0.0.1 'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
set +e
ssh "${opts[@]}" root@127.0.0.1 \
	'timeout --foreground 30s /tmp/ext4-evict-remount-natural /mnt/ext4-evict-natural; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; sync; umount /mnt/ext4-evict-natural; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 45); do
	if ! kill -0 "${qpid}" 2>/dev/null; then break; fi
	sleep 1
done
if kill -0 "${qpid}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	set +e
	e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' "${run_dir}" "${runtime_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'remount_|close_|workload_rc|model_(inode_bitmap|orphan_file_entries|xattr_block_refs)|error_exit_|violations_total|last_violation|bitmap differences|orphan|Extended attribute' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
