#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_GROUP_EXTEND_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
kernel_config="${EXT4_GROUP_EXTEND_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_GROUP_EXTEND_RACE_BASE_IMAGE:-/tmp/ext4-group-extend-shutdown-race.raw}"
target_blocks="${EXT4_GROUP_EXTEND_RACE_TARGET_BLOCKS:-65536}"
port="${EXT4_GROUP_EXTEND_RACE_PORT:-11728}"
run_dir="${EXT4_GROUP_EXTEND_RACE_RUN_DIR:-${work_dir}/ext4-group-extend-shutdown-race-$(date +%Y%m%d%H%M%S)-$$}"
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
program="${run_dir}/ext4-group-extend-shutdown-race"
gcc -O2 -Wall -Wextra -Werror -static -pthread -o "${program}" \
	"${root_dir}/tests/ext4-group-extend-shutdown-race.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 384M
	mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 16384 \
		-E lazy_itable_init=0 "${test_base}" 40000
fi
if ! dumpe2fs -h "${test_base}" 2>/dev/null | \
		rg -q 'Block count:[[:space:]]+40000'; then
	echo "${test_base} must contain a 40000-block ext4 filesystem" >&2
	exit 2
fi
e2fsck -fn "${test_base}" >"${run_dir}/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "${root_base}" \
	"${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" \
	"${run_dir}/test.qcow2"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=8192' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() {
	if kill -0 "${qemu_pid}" 2>/dev/null; then
		kill "${qemu_pid}" 2>/dev/null || true
	fi
}
trap cleanup EXIT

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-group-extend; mountpoint -q /mnt/ext4-group-extend || mount /dev/vdb /mnt/ext4-group-extend; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
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
	root@127.0.0.1:/tmp/ext4-group-extend-shutdown-race

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"/tmp/ext4-group-extend-shutdown-race /mnt/ext4-group-extend ${target_blocks}; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'EXT4 invariant violation|EXT4-fs.*error|WARNING:|BUG:' | tail -80 || true; sync; umount /mnt/ext4-group-extend || true; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" \
	"${run_dir}/recovered.raw"
set +e
e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
	>"${run_dir}/journal-replay.txt" 2>&1
journal_rc=$?
e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'run_dir=%s runtime_rc=%s journal_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${runtime_rc}" "${journal_rc}" "${fsck_rc}" \
	| tee "${run_dir}/report.txt"
rg -n 'before_blocks=|capacity_observed=|shutdown_rc=|group_extend_rc=|after_blocks=|snapshot OK|violations_total|snapshot_races|blocks_count|free_blocks_count|Block bitmap differences|Free .* count wrong' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" || true
if ! grep -Eq 'Block bitmap differences:|Free .* count wrong' \
	"${run_dir}/fsck.txt" &&
	! grep -Eq 'model_block_bitmap_violations=[1-9][0-9]*|violations_total=[1-9][0-9]*' \
	"${run_dir}/runtime.txt"; then
	echo 'group-extend reproduction did not observe a capacity/bitmap mismatch' >&2
	exit 1
fi
