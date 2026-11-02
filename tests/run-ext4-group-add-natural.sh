#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_GROUP_ADD_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
kernel_config="${EXT4_GROUP_ADD_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_GROUP_ADD_PORT:-11762}"
initial_blocks="${EXT4_GROUP_ADD_INITIAL_BLOCKS:-524288}"
group_blocks="${EXT4_GROUP_ADD_BLOCKS:-32768}"
inodes="${EXT4_GROUP_ADD_INODES:-131072}"
shutdown_race="${EXT4_GROUP_ADD_SHUTDOWN_RACE:-0}"
run_dir="${EXT4_GROUP_ADD_RUN_DIR:-${work_dir}/ext4-group-add-natural-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${kernel_config}" ||
	grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
	echo "kernel config must enable the checker and disable fault injection" >&2
	exit 2
fi
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing kernel or root image" >&2
	exit 2
fi
if [[ "${shutdown_race}" != 0 && "${shutdown_race}" != 1 ]]; then
	echo "EXT4_GROUP_ADD_SHUTDOWN_RACE must be 0 or 1" >&2
	exit 2
fi

mkdir -p "${run_dir}"
test_image="${run_dir}/test.raw"
program="${run_dir}/ext4-group-add-natural"
qemu_pid_file="${run_dir}/qemu.pid"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-group-add-natural.c"
qemu-img create -q -f raw "${test_image}" 4G
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N "${inodes}" \
	-J size=16 -E lazy_itable_init=0 "${test_image}" "${initial_blocks}"
fs_geometry="$(dumpe2fs -h "${test_image}" 2>/dev/null)"
blocks_per_group="$(awk -F: '/^Blocks per group:/ {gsub(/[[:space:]]/, "", $2); print $2}' <<<"${fs_geometry}")"
inodes_per_group="$(awk -F: '/^Inodes per group:/ {gsub(/[[:space:]]/, "", $2); print $2}' <<<"${fs_geometry}")"
block_size="$(awk -F: '/^Block size:/ {gsub(/[[:space:]]/, "", $2); print $2}' <<<"${fs_geometry}")"
inode_size="$(awk -F: '/^Inode size:/ {gsub(/[[:space:]]/, "", $2); print $2}' <<<"${fs_geometry}")"
if (( initial_blocks % blocks_per_group != 0 )); then
	echo "initial filesystem size must end at a group boundary" >&2
	exit 2
fi
group="${EXT4_GROUP_ADD_GROUP:-$((initial_blocks / blocks_per_group))}"
table_blocks="$(((inodes_per_group * inode_size + block_size - 1) / block_size))"
e2fsck -fn "${test_image}" >"${run_dir}/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "${root_base}" \
	"${run_dir}/root.qcow2"

cleanup() {
	if [[ -s "${qemu_pid_file}" ]]; then
		kill "$(<"${qemu_pid_file}")" 2>/dev/null || true
	fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid_file}"
qemu_pid="$(<"${qemu_pid_file}")"

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/group-add; mountpoint -q /mnt/group-add || mount /dev/vdb /mnt/group-add; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		mounted=1
		break
	fi
	sleep 1
done
if [[ "${mounted}" != 1 ]]; then
	echo "guest did not mount checker filesystem" >&2
	exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	>"${run_dir}/checker-before.txt"
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" \
	root@127.0.0.1:/tmp/ext4-group-add-natural

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"/tmp/ext4-group-add-natural /mnt/group-add ${group} ${initial_blocks} ${group_blocks} ${table_blocks} ${shutdown_race} 2; syscall_rc=\$?; echo syscall_rc=\${syscall_rc}; for i in 1 2 3; do cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; sleep 1; done; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | grep -E 'EXT4 invariant violation|EXT4-fs.*error|WARNING:|BUG:' | tail -100 || true; sync; umount /mnt/group-add; systemctl poweroff" \
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
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	if [[ "${shutdown_race}" == 1 ]]; then
		qemu-img convert -q -f raw -O raw "${test_image}" \
			"${run_dir}/recovered.raw"
		e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
			>"${run_dir}/journal-replay.txt" 2>&1 || true
		set +e
		e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
		fsck_rc=$?
		set -e
	else
		set +e
		e2fsck -fn "${test_image}" >"${run_dir}/fsck.txt" 2>&1
		fsck_rc=$?
		set -e
	fi
fi
dumpe2fs -h "${test_image}" >"${run_dir}/dumpe2fs.txt" 2>&1
{
	printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' \
		"${run_dir}" "${runtime_rc}" "${fsck_rc}"
	rg -n 'group_add|before_blocks=|after_blocks=|syscall_rc=|snapshot OK|violations_total|unverified_total|snapshot_races|group_capacity|resize_unverified' \
		"${run_dir}/runtime.txt" "${run_dir}/checker-before.txt" || true
	rg 'Block count:|Free blocks:|Inode count:|Free inodes:' \
		"${run_dir}/dumpe2fs.txt" || true
	tail -16 "${run_dir}/fsck.txt"
} | tee "${run_dir}/report.txt"
