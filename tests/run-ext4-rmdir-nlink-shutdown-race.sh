#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_RMDIR_NLINK_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
kernel_config="${EXT4_RMDIR_NLINK_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_RMDIR_NLINK_PORT:-11774}"
run_dir="${EXT4_RMDIR_NLINK_RUN_DIR:-${work_dir}/ext4-rmdir-nlink-shutdown-race-$(date +%Y%m%d%H%M%S)-$$}"
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

mkdir -p "${run_dir}"
program="${run_dir}/ext4-rmdir-nlink-shutdown-race"
qemu_pid_file="${run_dir}/qemu.pid"
test_image="${run_dir}/test.raw"
gcc -O2 -Wall -Wextra -Werror -static -pthread -o "${program}" \
	"${root_dir}/tests/ext4-rmdir-nlink-shutdown-race.c"
qemu-img create -q -f raw "${test_image}" 256M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 \
	-J size=4 -E lazy_itable_init=0 "${test_image}"
e2fsck -fn "${test_image}" >"${run_dir}/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "${root_base}" \
	"${run_dir}/root.qcow2"

cleanup() {
	if [[ -s "${qemu_pid_file}" ]]; then
		kill "$(<"${qemu_pid_file}")" 2>/dev/null || true
	fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid_file}"
qemu_pid="$(<"${qemu_pid_file}")"
cleanup() {
	if kill -0 "${qemu_pid}" 2>/dev/null; then
		kill "${qemu_pid}" 2>/dev/null || true
	fi
}
trap cleanup EXIT

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-rmdir-nlink; mountpoint -q /mnt/ext4-rmdir-nlink || mount /dev/vdb /mnt/ext4-rmdir-nlink; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
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
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" \
	root@127.0.0.1:/tmp/ext4-rmdir-nlink-shutdown-race

ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'/tmp/ext4-rmdir-nlink-shutdown-race /mnt/ext4-rmdir-nlink; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E "EXT4 invariant violation|EXT4-fs.*error|WARNING:|BUG:" | tail -100 || true; umount /mnt/ext4-rmdir-nlink || true; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1 || true
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
	qemu-img convert -q -f raw -O raw "${test_image}" \
		"${run_dir}/recovered.raw"
	e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
		>"${run_dir}/journal-replay.txt" 2>&1 || true
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s fsck_rc=%s\n' "${run_dir}" "${fsck_rc}" \
	| tee "${run_dir}/report.txt"
rg -n 'trigger_child|done_at_shutdown|shutdown_rc|parent_nlink|rmdir_child|snapshot OK|violations_total|last_violation|Directories count' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" || true
