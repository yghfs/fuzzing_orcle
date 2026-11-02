#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ext4-fault-env.sh"

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base_image="${EXT4_GROUP_ADD_BASE_IMAGE:-/tmp/ext4-group-add-base.raw}"
port="${EXT4_GROUP_ADD_PORT:-11541}"
repeat="${EXT4_GROUP_ADD_REPEATS:-3}"
run_dir="${work_dir}/ext4-group-add-failure-$(date +%Y%m%d%H%M%S)-$$"
program="${run_dir}/ext4-group-add-failure"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
active_pid_file=""

mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-group-add-failure.c"
if [[ ! -e "${test_base_image}" ]]; then
	qemu-img create -q -f raw "${test_base_image}" 4G
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 131072 \
		-E lazy_itable_init=0 "${test_base_image}" 524288
fi
if ! dumpe2fs -h "${test_base_image}" 2>/dev/null | \
		rg -q 'Block count:[[:space:]]+524288'; then
	echo "${test_base_image} must contain a 524288-block ext4 filesystem" >&2
	exit 2
fi
e2fsck -fn "${test_base_image}" > "${run_dir}/baseline-fsck.txt" 2>&1 || true

cleanup() {
	if [[ -n "${active_pid_file}" && -s "${active_pid_file}" ]]; then
		kill "$(<"${active_pid_file}")" 2>/dev/null || true
	fi
}
trap cleanup EXIT

run_case() {
	local label="$1"
	local fault="$2"
	local case_dir="${run_dir}/${label}"
	local root_overlay="${case_dir}/root.qcow2"
	local test_overlay="${case_dir}/test.qcow2"
	local recovered="/tmp/ext4-group-add-recovered-${label}-$$.raw"
	local pid_file="${case_dir}/qemu.pid"
	local serial_log="${case_dir}/serial.log"
	local qemu_pid=""
	local ssh_rc=0
	local journal_rc=125
	local fsck_rc=125

	mkdir -p "${case_dir}"
	qemu-img create -q -f qcow2 -F raw -b "${base_image}" \
		"${root_overlay}"
	qemu-img create -q -f qcow2 -F raw -b "${test_base_image}" \
		"${test_overlay}"
	active_pid_file="${pid_file}"
	qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
		-kernel "${EXT4_FAULT_KERNEL}" \
		-drive "file=${root_overlay},format=qcow2,if=virtio" \
		-drive "file=${test_overlay},format=qcow2,if=virtio" \
		-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
		-device virtio-net-pci,netdev=n0 \
		-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096" \
		-display none -monitor none -serial "file:${serial_log}" -daemonize \
		-pidfile "${pid_file}"
	qemu_pid="$(<"${pid_file}")"
	for _ in $(seq 1 120); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'mkdir -p /mnt/ext4-group-add; mountpoint -q /mnt/ext4-group-add || mount /dev/vdb /mnt/ext4-group-add; test -r /sys/kernel/debug/ext4/vdb/invariant_check && test -w /sys/module/ext4/parameters/ext4_fault_mode' \
			>/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mountpoint -q /mnt/ext4-group-add && test -r /sys/kernel/debug/ext4/vdb/invariant_check'
	scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		"printf ${fault} > /sys/module/ext4/parameters/ext4_fault_mode"
	set +e
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'/tmp/ext4-group-add-failure /mnt/ext4-group-add 16 524288 32768' \
		> "${case_dir}/program.txt" 2>&1
	ssh_rc=$?
	set -e
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'cat /sys/kernel/debug/ext4/vdb/invariant_check' \
		> "${case_dir}/checker.txt" 2>&1 || true
	ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg \
		> "${case_dir}/dmesg.txt" 2>&1 || true
	kill -9 "${qemu_pid}" 2>/dev/null || true
	for _ in $(seq 1 30); do
		if ! kill -0 "${qemu_pid}" 2>/dev/null; then
			break
		fi
		sleep 1
	done
	if kill -0 "${qemu_pid}" 2>/dev/null; then
		echo 'qemu did not stop after SIGKILL' > "${case_dir}/e2fsck.txt"
	else
		qemu-img convert -q -f qcow2 -O raw "${test_overlay}" "${recovered}"
		set +e
		e2fsck -E journal_only -y "${recovered}" \
			> "${case_dir}/journal-replay.txt" 2>&1
		journal_rc=$?
		e2fsck -fn "${recovered}" > "${case_dir}/e2fsck.txt" 2>&1
		fsck_rc=$?
		set -e
		rm -f "${recovered}"
	fi
	active_pid_file=""
	{
		printf '[%s] fault=%s ssh_rc=%s journal_rc=%s fsck_rc=%s\n' \
			"${label}" "${fault}" "${ssh_rc}" "${journal_rc}" "${fsck_rc}"
		cat "${case_dir}/program.txt"
		rg 'snapshot|violations_total|unverified_total|snapshot_races|tx_underflows|group_capacity|resize_unverified' \
			"${case_dir}/checker.txt" || true
		rg 'group-add superblock dirty failed|EXT4-fs.*error|WARNING:|BUG:|Oops' \
			"${case_dir}/dmesg.txt" || true
		tail -12 "${case_dir}/e2fsck.txt"
	} | tee "${case_dir}/report.txt"
}

run_case control 0
for n in $(seq 1 "${repeat}"); do
	run_case "fault-${n}" 8
done
echo "group-add failure test complete: ${run_dir}"
