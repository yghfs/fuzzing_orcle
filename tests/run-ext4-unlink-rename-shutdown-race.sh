#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_LINK_RACE_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
kernel_config="${EXT4_LINK_RACE_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
case_name="${EXT4_LINK_RACE_CASE:?set EXT4_LINK_RACE_CASE=unlink|rename|exchange|mkdir|link|control}"
port="${EXT4_LINK_RACE_PORT:-11775}"
run_dir="${EXT4_LINK_RACE_RUN_DIR:-${work_dir}/ext4-${case_name}-nlink-shutdown-race-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

if [[ "${case_name}" != unlink && "${case_name}" != rename &&
	"${case_name}" != exchange &&
	"${case_name}" != mkdir && "${case_name}" != link &&
	"${case_name}" != control ]]; then
	echo "case must be unlink, rename, exchange, mkdir, link, or control" >&2
	exit 2
fi
if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${kernel_config}" ||
	grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
	echo "kernel config must enable checker and disable ext4 fault injection" >&2
	exit 2
fi
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing kernel or root image" >&2
	exit 2
fi

mkdir -p "${run_dir}"
program="${run_dir}/ext4-unlink-rename-shutdown-race"
qemu_pid_file="${run_dir}/qemu.pid"
test_image="${run_dir}/test.raw"
gcc -O2 -Wall -Wextra -Werror -static -pthread -o "${program}" \
	"${root_dir}/tests/ext4-unlink-rename-shutdown-race.c"
qemu-img create -q -f raw "${test_image}" 256M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 \
	-J size=4 -E lazy_itable_init=0 "${test_image}"
e2fsck -fn "${test_image}" >"${run_dir}/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"

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
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

mounted=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-link-race; mountpoint -q /mnt/ext4-link-race || mount /dev/vdb /mnt/ext4-link-race; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
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
	root@127.0.0.1:/tmp/ext4-unlink-rename-shutdown-race
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"/tmp/ext4-unlink-rename-shutdown-race /mnt/ext4-link-race ${case_name}; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'EXT4 invariant violation|EXT4-fs.*error|WARNING:|BUG:' | tail -100 || true; umount /mnt/ext4-link-race || true; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1 || true
for _ in $(seq 1 60); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
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
printf 'case=%s run_dir=%s fsck_rc=%s\n' "${case_name}" "${run_dir}" \
	"${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'case=|operation_rc|path=|snapshot |violations_total|last_violation|model_directory_link_count|Inode .* has .* links' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" || true
