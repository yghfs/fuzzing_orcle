#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EVICT_RO_KERNEL:-${root_dir}/work/ext4-new-kbuild/arch/x86/boot/bzImage}"
config="${EXT4_EVICT_RO_CONFIG:-${root_dir}/work/ext4-new-kbuild/.config}"
root_base="${EXT4_EVICT_RO_ROOT:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_EVICT_RO_PORT:-11966}"
run_dir="${EXT4_EVICT_RO_RUN_DIR:-${work_dir}/ext4-evict-remount-ro-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${config}"
grep -q '^# CONFIG_EXT4_FAULT_INJECTION is not set$' "${config}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-evict-remount-ro.c"
qemu-img create -q -f raw "${run_dir}/test.raw" 256M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 \
	-J size=4 -E lazy_itable_init=0 "${run_dir}/test.raw"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu_pid_file="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768M -smp 2 \
	-kernel "${kernel}" -drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" -daemonize -pidfile "${qemu_pid_file}"
qpid="$(<"${qemu_pid_file}")"
cleanup() { kill "${qpid}" 2>/dev/null || true; }
trap cleanup EXIT
mounted=0
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-evict; mountpoint -q /mnt/ext4-evict || mount /dev/vdb /mnt/ext4-evict; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1; then
		mounted=1
		break
	fi
	sleep 1
done
if [[ "${mounted}" != 1 ]]; then exit 1; fi
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/workload" \
	root@127.0.0.1:/tmp/ext4-evict-remount-ro
ssh "${opts[@]}" root@127.0.0.1 'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
set +e
ssh "${opts[@]}" root@127.0.0.1 \
	'timeout --foreground 30s /tmp/ext4-evict-remount-ro /mnt/ext4-evict; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; sync; umount /mnt/ext4-evict; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 45); do kill -0 "${qpid}" 2>/dev/null || break; sleep 1; done
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
rg -n 'shutdown_|close_|workload_rc|model_inode_bitmap|error_exit_inode_free|violations_total|last_violation|snapshot|bitmap differences|Inode .*i_blocks' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
checker_hit=0
fsck_hit=0
if grep -Eq '^model_inode_bitmap_violations=[1-9][0-9]*$' \
	"${run_dir}/runtime.txt"; then
	checker_hit=1
fi
if grep -Eq '(^|: )Inode bitmap differences:' "${run_dir}/fsck.txt"; then
	fsck_hit=1
fi
if [[ "${checker_hit}" != 1 && "${fsck_hit}" != 1 ]]; then
	echo 'eviction reproduction did not observe an inode bitmap mismatch' >&2
	exit 1
fi
