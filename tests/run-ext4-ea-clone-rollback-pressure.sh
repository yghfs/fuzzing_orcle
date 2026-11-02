#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EA_CLONE_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
kernel_config="${EXT4_EA_CLONE_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_EA_CLONE_ROOT_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_EA_CLONE_BASE:-${work_dir}/ext4-ea-clone-1g.raw}"
port="${EXT4_EA_CLONE_PORT:-12795}"
qemu_mem="${EXT4_EA_CLONE_QEMU_MEM:-1024}"
pairs="${EXT4_EA_CLONE_PAIRS:-8}"
pressure_mib="${EXT4_EA_CLONE_PRESSURE_MIB:-600}"
min_free_kib="${EXT4_EA_CLONE_MIN_FREE_KIB:-131072}"
run_dir="${EXT4_EA_CLONE_RUN_DIR:-${work_dir}/ext4-ea-clone-rollback-pressure-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

if ! grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${kernel_config}" ||
	grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
	echo "kernel config must enable the checker and disable fault injection" >&2
	exit 2
fi
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing checker kernel or root image" >&2
	exit 2
fi
mkdir -p "${run_dir}"
program="${run_dir}/ext4-ea-clone-rollback-pressure"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-ea-clone-rollback-pressure.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mke2fs -q -F -t ext4 -O ea_inode,^orphan_file -b 4096 -N 32768 \
		-m 0 -J size=8 -E lazy_itable_init=0 "${test_base}"
fi
e2fsck -fn "${test_base}" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu_pid_file="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${qemu_mem}M" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor "unix:${run_dir}/qemu-monitor.sock,server=on,wait=off" \
	-serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid_file}"
qemu_pid="$(<"${qemu_pid_file}")"
clean_poweroff=0
cleanup() {
	if [[ "${clean_poweroff}" != 1 ]] && kill -0 "${qemu_pid}" 2>/dev/null; then
		printf 'system_powerdown\n' | nc -U -w 2 -q 0 "${run_dir}/qemu-monitor.sock" || true
	fi
}
trap cleanup EXIT

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-ea-clone; mountpoint -q /mnt/ext4-ea-clone || mount /dev/vdb /mnt/ext4-ea-clone; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
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
ssh "${ssh_opts[@]}" root@127.0.0.1 'test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"timeout --foreground 600s /tmp/ext4-ea-clone-rollback-pressure /mnt/ext4-ea-clone/workload ${pairs} ${pressure_mib} ${min_free_kib}; rc=\$?; echo workload_rc=\${rc}; exit \${rc}" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	>"${run_dir}/checker.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'dmesg | grep -E "xattr_clone_rollback|ea_inode_references|EXT4-fs.*error|Out of memory|Killed process|WARNING:|BUG:" | tail -300 || true' \
	>"${run_dir}/dmesg.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 120); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		clean_poweroff=1
		break
	fi
	sleep 1
done
if [[ "${clean_poweroff}" != 1 ]]; then
	echo 'guest did not power off cleanly; refusing offline fsck' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" \
		"${run_dir}/recovered.raw"
	set +e
	e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
		>"${run_dir}/journal.txt" 2>&1
	journal_rc=$?
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
{
	printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' \
		"${run_dir}" "${runtime_rc}" "${fsck_rc}"
	rg -n 'seeded_pairs|clone_cow|cow_pair|workload_rc|model_xattr_block_refs|model_ea_inode_references|xattr_clone_rollback|violations_total|last_violation' \
		"${run_dir}/runtime.txt" "${run_dir}/checker.txt" \
		"${run_dir}/dmesg.txt" || true
	tail -30 "${run_dir}/fsck.txt"
} | tee "${run_dir}/report.txt"
