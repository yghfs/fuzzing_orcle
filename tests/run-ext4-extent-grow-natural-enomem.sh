#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EXTGROW_NATURAL_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
kernel_config="${EXT4_EXTGROW_NATURAL_CONFIG:-${root_dir}/work/ext4-new-kbuild/.config}"
root_base="${EXT4_EXTGROW_NATURAL_ROOT_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_EXTGROW_NATURAL_PORT:-11943}"
qemu_mem="${EXT4_EXTGROW_NATURAL_QEMU_MEM:-768}"
limits="${EXT4_EXTGROW_NATURAL_LIMITS:-4194304,8388608,12582912,16777216,25165824,33554432,50331648}"
run_dir="${EXT4_EXTGROW_NATURAL_RUN_DIR:-${work_dir}/ext4-extent-grow-natural-enomem-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' \
	"${kernel_config}"; then
	echo "kernel config must enable CONFIG_EXT4_INVARIANT_CHECKER" >&2
	exit 2
fi
if grep -q '^CONFIG_EXT4_FAULT_INJECTION=y$' \
	"${kernel_config}"; then
	echo "natural discovery run refuses CONFIG_EXT4_FAULT_INJECTION=y" >&2
	exit 2
fi
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing normal checker kernel or root image" >&2
	exit 2
fi

mkdir -p "${run_dir}"
program="${run_dir}/ext4-extent-grow-natural-enomem"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-extent-grow-natural-enomem.c"
test_image="${run_dir}/test.raw"
qemu-img create -q -f raw "${test_image}" 256M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 \
	-J size=4 -E lazy_itable_init=0 "${test_image}"
e2fsck -fn "${test_image}" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" \
	"${run_dir}/root.qcow2"

qemu_pid_file="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${qemu_mem}M" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid_file}"
qemu_pid="$(<"${qemu_pid_file}")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-extent-grow; mountpoint -q /mnt/ext4-extent-grow || mount /dev/vdb /mnt/ext4-extent-grow; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
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
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; sysctl -w vm.overcommit_memory=1 >/dev/null; echo 3 > /proc/sys/vm/drop_caches'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" \
	root@127.0.0.1:/tmp/ext4-extent-grow-natural-enomem
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"timeout --foreground 180s /tmp/ext4-extent-grow-natural-enomem /mnt/ext4-extent-grow '${limits}'; workload_rc=\$?; echo workload_rc=\${workload_rc}; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'EXT4 invariant violation|extent|Out of memory|Killed process|WARNING:|BUG:' | tail -160; sync; umount /mnt/ext4-extent-grow; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then break; fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	set +e
	e2fsck -E journal_only -y "${test_image}" >"${run_dir}/journal.txt" 2>&1
	journal_rc=$?
	e2fsck -fn "${test_image}" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
{
	printf 'run_dir=%s runtime_rc=%s fsck_rc=%s limits=%s\n' \
		"${run_dir}" "${runtime_rc}" "${fsck_rc}" "${limits}"
	rg -n 'attempt=|workload_rc|model_extent_blocks|error_exit_extent_map|violations_total|last_violation|EXT4 invariant violation|count wrong|bitmap differences' \
		"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" || true
	tail -25 "${run_dir}/fsck.txt"
} | tee "${run_dir}/report.txt"
