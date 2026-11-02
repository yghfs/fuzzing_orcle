#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_RESIZE_TEST_BASE:-${work_dir}/ext4-mkdir-credit-128m.raw}"
kernel="${EXT4_RESIZE_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
port="${EXT4_RESIZE_PORT:-11937}"
pressure_mb="${EXT4_RESIZE_PRESSURE_MB:-192}"
memcg_limit_mb="${EXT4_RESIZE_MEMCG_LIMIT_MB:-0}"
min_free_kb="${EXT4_RESIZE_MIN_FREE_KB:-131072}"
new_blocks="${EXT4_RESIZE_BLOCKS:-61312}"
run_dir="${EXT4_RESIZE_RUN_DIR:-${work_dir}/ext4-group-extend-natural-$(date +%Y%m%d%H%M%S)-$$}"
pid_file="${run_dir}/qemu.pid"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/group-extend" \
	"${root_dir}/tests/ext4-group-extend-natural.c"
cp --reflink=auto "${test_base}" "${run_dir}/test.raw"
truncate -s 256M "${run_dir}/test.raw"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/baseline-fsck.txt" 2>&1

cleanup() {
	if [[ -n "${qemu_pid_value:-}" ]]; then
		kill "${qemu_pid_value}" 2>/dev/null || true
	fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768M -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${pid_file}"
qemu_pid_value="$(<"${pid_file}")"

for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-resize; mountpoint -q /mnt/ext4-resize || mount /dev/vdb /mnt/ext4-resize; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"mountpoint -q /mnt/ext4-resize && sysctl -w vm.overcommit_memory=1 >/dev/null && sysctl -w vm.min_free_kbytes=${min_free_kb} >/dev/null && printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset"
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/group-extend" \
	root@127.0.0.1:/root/group-extend

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"/root/group-extend /mnt/ext4-resize ${new_blocks} ${pressure_mb} ${memcg_limit_mb}; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | tail -160; sync; umount /mnt/ext4-resize; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do
	if ! kill -0 "${qemu_pid_value}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid_value}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	set +e
	e2fsck -E journal_only -y "${run_dir}/test.raw" \
		>"${run_dir}/journal.txt" 2>&1
	journal_rc=$?
	e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
{
	printf 'run_dir=%s runtime_rc=%s fsck_rc=%s pressure_mb=%s memcg_limit_mb=%s min_free_kb=%s\n' \
		"${run_dir}" "${runtime_rc}" "${fsck_rc}" \
		"${pressure_mb}" "${memcg_limit_mb}" "${min_free_kb}"
	rg -n 'pressure_populated|group_extend|statfs_blocks|model_block_bitmap|violations_total|last_violation|snapshot|EXT4 invariant violation|EXT4-fs.*error' \
		"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" "${run_dir}/journal.txt" || true
} | tee "${run_dir}/report.txt"
