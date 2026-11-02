#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EA_ARRAY_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
kernel_config="${EXT4_EA_ARRAY_CONFIG:-${root_dir}/work/ext4-new-kbuild/.config}"
root_base="${EXT4_EA_ARRAY_ROOT_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_EA_ARRAY_BASE:-${work_dir}/ext4-ea-ref-array-1g.raw}"
port="${EXT4_EA_ARRAY_PORT:-11913}"
qemu_mem="${EXT4_EA_ARRAY_QEMU_MEM:-768}"
locked_mib="${EXT4_EA_ARRAY_LOCKED_MIB:-350}"
min_free_kib="${EXT4_EA_ARRAY_MIN_FREE_KIB:-131072}"
memcg_max="${EXT4_EA_ARRAY_MEMCG_MAX:-}"
memcg_fill_mib="${EXT4_EA_ARRAY_MEMCG_FILL_MIB:-0}"
memcg_fill_kib="${EXT4_EA_ARRAY_MEMCG_FILL_KIB:-$((memcg_fill_mib * 1024))}"
files="${EXT4_EA_ARRAY_FILES:-128}"
ea_per_file="${EXT4_EA_ARRAY_PER_FILE:-64}"
drop_caches="${EXT4_EA_ARRAY_DROP_CACHES:-0}"
timeout_sec="${EXT4_EA_ARRAY_TIMEOUT:-1800}"
run_dir="${EXT4_EA_ARRAY_RUN_DIR:-${work_dir}/ext4-ea-ref-array-pressure-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${kernel_config}" ||
	! grep -q '^# CONFIG_EXT4_FAULT_INJECTION is not set$' "${kernel_config}"; then
	echo "kernel config must enable the checker and disable fault injection" >&2
	exit 2
fi
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing checker kernel or root image" >&2
	exit 2
fi
mkdir -p "${run_dir}"
program="${run_dir}/ext4-ea-ref-array-memory-pressure"
gcc -O2 -Wall -Wextra -Werror -static -DEA_COUNT="${ea_per_file}" -o "${program}" \
	"${root_dir}/tests/ext4-ea-ref-array-memory-pressure.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mke2fs -q -F -t ext4 -O ea_inode,^orphan_file -b 4096 -N 32768 \
		-m 0 -J size=8 -E lazy_itable_init=0 "${test_base}"
fi
if ! dumpe2fs -h "${test_base}" 2>/dev/null | grep -q 'Filesystem features:.*ea_inode'; then
	echo "EA inode feature missing from ${test_base}" >&2
	exit 2
fi
e2fsck -fn "${test_base}" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu_pid="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${qemu_mem}M" -smp 2 \
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

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-ea-array; mountpoint -q /mnt/ext4-ea-array || mount /dev/vdb /mnt/ext4-ea-array; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
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
	"EXT4_EA_ARRAY_MEMCG_MAX=${memcg_max} EXT4_EA_ARRAY_MEMCG_FILL_KIB=${memcg_fill_kib} EXT4_EA_ARRAY_DROP_CACHES=${drop_caches} timeout --foreground ${timeout_sec}s /tmp/ext4-ea-ref-array-memory-pressure /mnt/ext4-ea-array/workload ${files} ${locked_mib} ${min_free_kib}; rc=\$?; echo workload_rc=\${rc}; exit \${rc}" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	>"${run_dir}/checker.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'dmesg | grep -E "xattr_container_release|EA inode|ea_inode|EXT4-fs.*error|Out of memory|Killed process|WARNING:|BUG:" | tail -300 || true' \
	>"${run_dir}/dmesg.txt" 2>&1
ssh "${ssh_opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do
	kill -0 "${qemu_pid_value}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid_value}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
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
	rg -n 'seeded_files|ea_array_unlink|unlinked_files|workload_rc|model_ea_references|source=xattr_container_release|violations_total|last_violation' \
		"${run_dir}/runtime.txt" "${run_dir}/checker.txt" "${run_dir}/dmesg.txt" || true
	tail -30 "${run_dir}/fsck.txt"
} | tee "${run_dir}/report.txt"
