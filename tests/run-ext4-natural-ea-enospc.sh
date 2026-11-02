#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_NATURAL_EA_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_NATURAL_EA_BASE:-/tmp/ext4-natural-ea-enospc.raw}"
port="${EXT4_NATURAL_EA_PORT:-11642}"
run_dir="${EXT4_NATURAL_EA_RUN_DIR:-${work_dir}/ext4-natural-ea-enospc-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
mkdir -p "${run_dir}"
program="${run_dir}/ext4-natural-ea-enospc"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-natural-ea-enospc.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mke2fs -q -F -t ext4 -O ea_inode,^orphan_file -b 4096 -N 32768 \
		-E lazy_itable_init=0 "${test_base}" 262144
fi
overlay="${run_dir}/root.qcow2"
test_overlay="${run_dir}/test.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${overlay}"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${test_overlay}"
qemu_pid="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${overlay},format=qcow2,if=virtio" \
	-drive "file=${test_overlay},format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" -daemonize \
	-pidfile "${qemu_pid}"
cleanup() {
	if [[ -s "${qemu_pid}" ]]; then
		kill "$(<"${qemu_pid}")" 2>/dev/null || true
	fi
}
trap cleanup EXIT
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-natural-ea; mountpoint -q /mnt/ext4-natural-ea || mount /dev/vdb /mnt/ext4-natural-ea; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1; then
		break
	fi
	sleep 1
done
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/
ssh "${opts[@]}" root@127.0.0.1 'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
set +e
ssh "${opts[@]}" root@127.0.0.1 \
	'timeout --foreground 180s /tmp/ext4-natural-ea-enospc /mnt/ext4-natural-ea/target' \
	>"${run_dir}/program.txt" 2>&1
program_rc=$?
set -e
ssh "${opts[@]}" root@127.0.0.1 'cat /sys/kernel/debug/ext4/vdb/invariant_summary' >"${run_dir}/checker.txt" 2>&1 || true
ssh "${opts[@]}" root@127.0.0.1 'cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | tail -120' >"${run_dir}/evidence.txt" 2>&1 || true
ssh "${opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do
	[[ -s "${qemu_pid}" ]] || break
	kill -0 "$(<"${qemu_pid}")" 2>/dev/null || break
	sleep 1
done
qemu-img convert -q -f qcow2 -O raw "${test_overlay}" "${run_dir}/recovered.raw"
set +e
e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'run_dir=%s program_rc=%s fsck_rc=%s\n' "${run_dir}" "${program_rc}" "${fsck_rc}"
rg -n 'fill_|remove_|model_|violations_total|last_violation|snapshot|EXT4-fs.*error' \
	"${run_dir}/program.txt" "${run_dir}/checker.txt" "${run_dir}/evidence.txt" || true
