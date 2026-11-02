#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_GROUP_EXTEND_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_GROUP_EXTEND_BASE_IMAGE:-/tmp/ext4-group-extend-natural.raw}"
port="${EXT4_GROUP_EXTEND_PORT:-11628}"
qemu_mem="${EXT4_GROUP_EXTEND_QEMU_MEM:-384}"
pressure_mb="${EXT4_GROUP_EXTEND_PRESSURE_MB:-96}"
target_blocks="${EXT4_GROUP_EXTEND_TARGET_BLOCKS:-50000}"
run_dir="${EXT4_GROUP_EXTEND_RUN_DIR:-${work_dir}/ext4-group-extend-natural-$(date +%Y%m%d%H%M%S)-$$}"
mkdir -p "${run_dir}"
program="${run_dir}/ext4-group-extend-natural-pressure"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-group-extend-natural-pressure.c"

if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 384M
	mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 16384 \
		-E lazy_itable_init=0 "${test_base}" 40000
fi
if ! dumpe2fs -h "${test_base}" 2>/dev/null | \
		rg -q 'Block count:[[:space:]]+40000'; then
	echo "${test_base} must contain a 40000-block ext4 filesystem" >&2
	exit 2
fi
e2fsck -fn "${test_base}" > "${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${qemu_mem}" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() {
	if kill -0 "${qemu_pid}" 2>/dev/null; then
		kill "${qemu_pid}" 2>/dev/null || true
	fi
}
trap cleanup EXIT

ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes \
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-group-extend; mountpoint -q /mnt/ext4-group-extend || mount /dev/vdb /mnt/ext4-group-extend; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test -r /sys/kernel/debug/ext4/vdb/invariant_summary && test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${program}" root@127.0.0.1:/tmp/
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; sysctl -w vm.overcommit_memory=1 >/dev/null; echo 3 > /proc/sys/vm/drop_caches'

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"set +e; /tmp/ext4-group-extend-natural-pressure /mnt/ext4-group-extend ${target_blocks} ${pressure_mb}; workload_rc=\$?; echo workload_rc=\${workload_rc}; set -e; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | tail -120; sync; systemctl poweroff" \
	> "${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 45); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" "${run_dir}/recovered.raw"
set +e
e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
	> "${run_dir}/journal-replay.txt" 2>&1
journal_rc=$?
e2fsck -fn "${run_dir}/recovered.raw" > "${run_dir}/fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'run_dir=%s runtime_rc=%s journal_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${runtime_rc}" "${journal_rc}" "${fsck_rc}" \
	| tee "${run_dir}/report.txt"
rg -n 'pressure_|group_extend_|workload_rc|model_block_bitmap|violations_total|last_violation|snapshot|Block bitmap differences|Free .* count wrong|EXT4-fs.*error|Out of memory|Killed process' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" || true
