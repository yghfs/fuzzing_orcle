#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_NATURAL_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_NATURAL_BASE:-${work_dir}/ext4-natural-pressure-1g.raw}"
quota_mode="${EXT4_NATURAL_QUOTA_MODE:-0}"
port="${EXT4_NATURAL_PORT:-11669}"
run_dir="${EXT4_NATURAL_RUN_DIR:-${work_dir}/ext4-natural-pressure-$(date +%Y%m%d%H%M%S)-$$}"
mkdir -p "${run_dir}"
program="${run_dir}/ext4-natural-pressure-workload"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-natural-pressure-workload.c"
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing kernel or root image" >&2
	exit 2
fi
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mkfs_features=(-O ^orphan_file)
	if [[ "${quota_mode}" != 0 ]]; then
		mkfs_features=(-O quota,^orphan_file)
	fi
	mke2fs -q -F -t ext4 "${mkfs_features[@]}" -b 4096 -N 32768 \
		-E lazy_itable_init=0 "${test_base}"
fi
root_overlay="${run_dir}/root.qcow2"
test_overlay="${run_dir}/test.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${root_overlay}"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${test_overlay}"
qemu_pid="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${root_overlay},format=qcow2,if=virtio" \
	-drive "file=${test_overlay},format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid}"
qemu_pid_value="$(<"${qemu_pid}")"
cleanup() {
	if [[ -s "${qemu_pid}" ]]; then kill "$(<"${qemu_pid}")" 2>/dev/null || true; fi
}
trap cleanup EXIT
opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes \
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
mount_options=""
if [[ "${quota_mode}" != 0 ]]; then
	mount_options="-o usrquota"
fi
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 \
		"mkdir -p /mnt/ext4-natural-pressure; mountpoint -q /mnt/ext4-natural-pressure || mount ${mount_options} /dev/vdb /mnt/ext4-natural-pressure; test -r /sys/kernel/debug/ext4/vdb/invariant_summary" \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode && mount -o remount,commit=1 /mnt/ext4-natural-pressure'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${program}" root@127.0.0.1:/tmp/ext4-natural-pressure-workload
ssh "${opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
set +e
ssh "${opts[@]}" root@127.0.0.1 \
	"timeout --foreground 900s /tmp/ext4-natural-pressure-workload /mnt/ext4-natural-pressure ${quota_mode} /dev/vdb > /tmp/ext4-natural-pressure.log 2>&1; echo workload_rc=\$?; cat /tmp/ext4-natural-pressure.log" \
	>"${run_dir}/program.txt" 2>&1
program_ssh_rc=$?
set -e
ssh "${opts[@]}" root@127.0.0.1 \
	'cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	>"${run_dir}/checker.txt" 2>&1 || true
ssh "${opts[@]}" root@127.0.0.1 \
	'cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | tail -200' \
	>"${run_dir}/evidence.txt" 2>&1 || true
ssh "${opts[@]}" root@127.0.0.1 \
	'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do
	kill -0 "${qemu_pid_value}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid_value}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	qemu-img convert -q -f qcow2 -O raw "${test_overlay}" "${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s program_ssh_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${program_ssh_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'pressure_|natural_pressure|ENOSPC|EIO|model_|violations_total|last_violation|snapshot|error' \
	"${run_dir}/program.txt" "${run_dir}/checker.txt" "${run_dir}/evidence.txt" || true
tail -20 "${run_dir}/fsck.txt"
