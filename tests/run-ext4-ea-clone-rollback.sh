#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${EXT4_EA_CLONE_RUN_DIR:-${work_dir}/ext4-ea-clone-rollback-$(date +%Y%m%d%H%M%S)-$$}"
kernel="${EXT4_EA_CLONE_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
config="${EXT4_EA_CLONE_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_EA_CLONE_ROOT_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_EA_CLONE_BASE:-${work_dir}/ext4-ea-ref-array-1g.raw}"
port="${EXT4_EA_CLONE_PORT:-11927}"
memory="${EXT4_EA_CLONE_QEMU_MEMORY:-768}"
rounds="${EXT4_EA_CLONE_ROUNDS:-32}"
pressure="${EXT4_EA_CLONE_PRESSURE_MIB:-176}"
count="${EXT4_EA_CLONE_COUNT:-64}"
key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${config}"
grep -q '^# CONFIG_EXT4_FAULT_INJECTION is not set$' "${config}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -DEA_COUNT="${count}" \
	-o "${run_dir}/ext4-ea-clone-rollback" \
	"${root_dir}/tests/ext4-ea-clone-rollback.c"
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
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-ea-clone; mountpoint -q /mnt/ext4-ea-clone || mount /dev/vdb /mnt/ext4-ea-clone; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 'test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/ext4-ea-clone-rollback" \
	root@127.0.0.1:/tmp/
ssh "${ssh_opts[@]}" root@127.0.0.1 'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"timeout --foreground 600s /tmp/ext4-ea-clone-rollback /mnt/ext4-ea-clone/work ${rounds} ${pressure}; rc=\$?; echo workload_rc=\$rc; exit \$rc" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	>"${run_dir}/checker.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'dmesg | grep -E "xattr_clone_rollback|cleanup ea_ino|model=ea_references|Out of memory|Killed process|EXT4-fs.*error|WARNING:|BUG:" | tail -300 || true' \
	>"${run_dir}/dmesg.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" \
		"${run_dir}/recovered.raw"
	e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
		>"${run_dir}/journal.txt" 2>&1 || true
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${runtime_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'ea_clone|clone_update_round|workload_rc|model_ea_references|source=xattr_clone_rollback|violations_total|last_violation|cleanup ea_ino' \
	"${run_dir}/runtime.txt" "${run_dir}/checker.txt" "${run_dir}/dmesg.txt" \
	| tee -a "${run_dir}/report.txt" || true
tail -30 "${run_dir}/fsck.txt"
