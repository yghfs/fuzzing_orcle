#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_DIR_NLINK_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
kernel_config="${EXT4_DIR_NLINK_CONFIG:-$(dirname "${kernel}")/config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_DIR_NLINK_BASE:-${work_dir}/ext4-dir-nlink-1g.raw}"
port="${EXT4_DIR_NLINK_PORT:-11761}"
children="${EXT4_DIR_NLINK_CHILDREN:-65520}"
run_dir="${EXT4_DIR_NLINK_RUN_DIR:-${work_dir}/ext4-dir-nlink-overflow-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"

if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${kernel_config}" ||
	grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
	echo "kernel config must enable the checker and disable fault injection" >&2
	exit 2
fi
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing kernel or root image" >&2
	exit 2
fi
mkdir -p "${run_dir}"
program="${run_dir}/ext4-dir-nlink-overflow"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-dir-nlink-overflow.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 131072 \
		-J size=16 -E lazy_itable_init=0 "${test_base}"
fi
if ! dumpe2fs -h "${test_base}" 2>/dev/null | rg -q 'Filesystem features:.*dir_nlink'; then
	echo "test image must enable the ext4 dir_nlink feature" >&2
	exit 2
fi
e2fsck -fn "${test_base}" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4 \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/dir-nlink; mountpoint -q /mnt/dir-nlink || mount /dev/vdb /mnt/dir-nlink; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; mount -o remount,commit=1 /mnt/dir-nlink; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/ext4-dir-nlink-overflow
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"rm -f /tmp/ext4-dir-nlink-ready /tmp/ext4-dir-nlink-rc; (timeout --foreground 1200s /tmp/ext4-dir-nlink-overflow /mnt/dir-nlink/parent ${children} 300; echo \$? >/tmp/ext4-dir-nlink-rc) >/tmp/ext4-dir-nlink.log 2>&1 </dev/null &"
program_ssh_rc=$?
set -e
if [[ "${program_ssh_rc}" != 0 ]]; then
	echo "failed to start directory workload: ${program_ssh_rc}" >&2
	exit 1
fi
ready=0
for _ in $(seq 1 900); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'test -e /tmp/ext4-dir-nlink-ready' >/dev/null 2>&1; then
		ready=1
		break
	fi
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'test ! -e /tmp/ext4-dir-nlink-rc' >/dev/null 2>&1 || break
	sleep 1
done
if [[ "${ready}" != 1 ]]; then
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'cat /tmp/ext4-dir-nlink.log 2>/dev/null || true' \
		>"${run_dir}/program.txt" 2>&1 || true
	echo "directory workload did not reach the saturated state" >&2
	exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'cat /tmp/ext4-dir-nlink.log; for i in 1 2 3; do cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; sleep 1; done; cat /sys/kernel/debug/ext4/vdb/invariant_summary; touch /tmp/ext4-dir-nlink-release' \
	>"${run_dir}/checker-full.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'for i in $(seq 1 1200); do test -e /tmp/ext4-dir-nlink-rc && break; sleep 1; done; cat /tmp/ext4-dir-nlink.log; printf "workload_rc=%s\n" "$(cat /tmp/ext4-dir-nlink-rc 2>/dev/null || echo 124)"' \
	>"${run_dir}/program.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'for i in 1 2 3; do cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; sleep 1; done; cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	>"${run_dir}/checker.txt" 2>&1 || true
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'dmesg | grep -E "EXT4-fs.*error|BUG:|WARNING:" | tail -100 || true; sync; systemctl poweroff' \
	>"${run_dir}/evidence.txt" 2>&1 || true
for _ in $(seq 1 45); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" \
		"${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s program_ssh_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${program_ssh_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'dir_nlink_|mkdir_progress|workload_rc|snapshot|violations_total|unverified_total|snapshot_races|model_' \
	"${run_dir}/program.txt" "${run_dir}/checker-full.txt" \
	"${run_dir}/checker.txt" "${run_dir}/evidence.txt" \
	| tee -a "${run_dir}/report.txt" || true
tail -20 "${run_dir}/fsck.txt"
