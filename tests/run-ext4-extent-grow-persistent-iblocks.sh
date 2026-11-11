#!/usr/bin/env bash
# Precise natural memory pressure extent tree growth with persistent i_blocks errors
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
build="$(cd "${root_dir}/experiments/ext4-invariant-checker/build/linux-invariant-checker" && pwd)"
kernel="${build}/arch/x86/boot/bzImage"
config="${build}/.config"
root_base="${work_dir}/debian-clean-smalljournal.img"
port=11945
qemu_mem=1024
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
run_dir="${1:-${work_dir}/ext4-extent-grow-persistent-iblocks-$(date +%Y%m%d-%H%M%S)}"

mkdir -p "${run_dir}"

echo "=== Checking prerequisites ==="
grep -q "^CONFIG_EXT4_INVARIANT_CHECKER=y$" "${config}" || { echo "CONFIG_EXT4_INVARIANT_CHECKER required"; exit 2; }
grep -q "^# CONFIG_EXT4_FAULT_INJECTION is not set$" "${config}" || { echo "Must disable CONFIG_EXT4_FAULT_INJECTION"; exit 2; }
[[ -r "${kernel}" && -r "${root_base}" ]] || { echo "Missing kernel or root image"; exit 2; }

echo "=== Building workload ==="
gcc -O2 -Wall -Wextra -Werror -static -pthread -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-extent-grow-persistent-iblocks.c"

echo "=== Setting up test image ==="
test_image="${run_dir}/test.raw"
qemu-img create -q -f raw "${test_image}" 512M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 16384 \
	-J size=4 -E lazy_itable_init=0 "${test_image}"
e2fsck -fn "${test_image}" >"${run_dir}/baseline-fsck.txt" 2>&1 || true

echo "=== Setting up root image ==="
root_prep="${run_dir}/root-base.raw"
cp --sparse=always "${root_base}" "${root_prep}"
tune2fs -O ^orphan_file "${root_prep}" >/dev/null 2>&1
qemu_root="${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_prep}" "${qemu_root}"

echo "=== Starting QEMU ==="
qemu_pid_file="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${qemu_mem}M" -smp 4 \
	-kernel "${kernel}" \
	-drive "file=${qemu_root},format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid_file}"
qemu_pid="$(<"${qemu_pid_file}")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

echo "=== Waiting for guest boot ==="
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-test; mount /dev/vdb /mnt/ext4-test 2>/dev/null || true; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done

echo "=== Preparing checker ==="
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'echo 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; sysctl -w vm.overcommit_memory=1 >/dev/null; echo 3 > /proc/sys/vm/drop_caches'

echo "=== Uploading workload ==="
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/workload" root@127.0.0.1:/tmp/extent-grow

echo "=== Running extent grow workload (files KEPT for fsck) ==="
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'timeout --foreground 300s /tmp/extent-grow /mnt/ext4-test 256 20 ; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E "EXT4|extent|Out of memory" | tail -100; sync; umount /mnt/ext4-test; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e

echo "=== Waiting for guest shutdown ==="
for _ in $(seq 1 60); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done

if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo "WARNING: Guest did not power off cleanly"
	fsck_rc=125
	echo "guest did not power off cleanly" >"${run_dir}/fsck.txt"
else
	echo "=== Running offline checks ==="
	set +e
	e2fsck -E journal_only -y "${test_image}" >"${run_dir}/journal.txt" 2>&1
	e2fsck -fn "${test_image}" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi

echo "=== Results ==="
{
	printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' "${run_dir}" "${runtime_rc}" "${fsck_rc}"
	echo "=== Inode i_blocks errors (persistent) ==="
	grep "i_blocks" "${run_dir}/fsck.txt" | head -30 || echo "No i_blocks errors found"
	echo "=== Block bitmap differences ==="
	grep "块位图" "${run_dir}/fsck.txt" | head -10 || echo "No bitmap differences"
	echo "=== Checker violations ==="
	grep "model_extent_blocks_violations" "${run_dir}/runtime.txt" | tail -2
} | tee "${run_dir}/report.txt"

echo "Evidence preserved in: $run_dir"
