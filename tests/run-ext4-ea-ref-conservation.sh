#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${EXT4_EA_REF_RUN_DIR:-${work_dir}/ext4-ea-ref-conservation-$(date +%Y%m%d%H%M%S)-$$}"
kernel="${EXT4_EA_REF_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
config="${EXT4_EA_REF_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_EA_REF_ROOT_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_EA_REF_BASE:-${work_dir}/ext4-ea-ref-array-1g.raw}"
port="${EXT4_EA_REF_PORT:-12797}"
memory="${EXT4_EA_REF_MEMORY:-512}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${config}"
grep -qx '# CONFIG_EXT4_FAULT_INJECTION is not set' "${config}"
test -r "${kernel}"
test -r "${root_base}"
test -r "${test_base}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static \
	-o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-ea-ref-conservation.c"
e2fsck -fn "${test_base}" >"${run_dir}/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${memory}M" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"

for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ea-ref; mountpoint -q /mnt/ea-ref || mount /dev/vdb /mnt/ea-ref; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/workload" \
	root@127.0.0.1:/tmp/ea-ref-conservation
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; /tmp/ea-ref-conservation /mnt/ea-ref/work 1 0; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary; sync; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e

qemu_pid="$(<"${run_dir}/qemu.pid")"
for _ in $(seq 1 90); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo 'guest did not power off cleanly; offline check refused' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" \
		"${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${runtime_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'workload_rc|model=ea_inode_references|model_ea_inode_references|violations_total|last_violation|Extended attribute inode|ref count is' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
test "${runtime_rc}" -eq 0
test "${fsck_rc}" -ne 0
