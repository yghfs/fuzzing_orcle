#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_MKDIR_CREDIT_KERNEL:-${work_dir}/kernels/ext4-natural-journal-telemetry/bzImage}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_MKDIR_CREDIT_BASE:-${work_dir}/ext4-mkdir-credit-128m.raw}"
port="${EXT4_MKDIR_CREDIT_PORT:-11931}"
seed="${EXT4_MKDIR_CREDIT_SEED:-20000}"
loops="${EXT4_MKDIR_CREDIT_LOOPS:-30000}"
run_dir="${EXT4_MKDIR_CREDIT_RUN_DIR:-${work_dir}/ext4-mkdir-credit-natural-$(date +%Y%m%d%H%M%S)-$$}"
mkdir -p "${run_dir}"
program="${run_dir}/ext4-mkdir-credit-stress"
qemu_pid="${run_dir}/qemu.pid"

gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-mkdir-credit-stress.c"
if [[ ! -e "${test_base}" ]]; then
	truncate -s 128M "${test_base}"
	mke2fs -q -F -t ext4 -b 4096 -N 65536 -m 0 \
		-O 'extent,^64bit,^orphan_file' -J size=4 \
		-E lazy_itable_init=0 "${test_base}"
fi
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768M -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid}"
qemu_pid_value="$(<"${qemu_pid}")"
cleanup() { kill "${qemu_pid_value}" 2>/dev/null || true; }
trap cleanup EXIT
ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes \
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	-o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-mkdir-credit; mountpoint -q /mnt/ext4-mkdir-credit || mount -o commit=1 /dev/vdb /mnt/ext4-mkdir-credit; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${program}" root@127.0.0.1:/tmp/ext4-mkdir-credit-stress
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"timeout --foreground 900s /tmp/ext4-mkdir-credit-stress /mnt/ext4-mkdir-credit/tree ${seed} ${loops}; rc=\$?; echo workload_rc=\${rc}; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | tail -300; sync; umount /mnt/ext4-mkdir-credit; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 45); do
	kill -0 "${qemu_pid_value}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid_value}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" "${run_dir}/recovered.raw"
	set +e
	e2fsck -E journal_only -y "${run_dir}/recovered.raw" >"${run_dir}/journal.txt" 2>&1
	journal_rc=$?
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
	printf 'journal_rc=%s\n' "${journal_rc}" >>"${run_dir}/journal.txt"
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s seed=%s loops=%s\n' \
	"${run_dir}" "${runtime_rc}" "${fsck_rc}" "${seed}" "${loops}" \
	| tee "${run_dir}/report.txt"
rg -n 'mkdir_credit|model_mkdir_atomicity|model_directory_link_count|violations_total|last_violation|EXT4 invariant violation|credits|WARNING|EXT4-fs.*error' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" "${run_dir}/journal.txt" || true
