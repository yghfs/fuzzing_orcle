#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ext4-fault-env.sh"

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base_image="${EXT4_RESIZE_BASE_IMAGE:-/tmp/ext4-resize-failure-noorphan.img}"
port="${EXT4_RESIZE_PORT:-11440}"
target_blocks="${EXT4_RESIZE_TARGET_BLOCKS:-505000}"
repeat="${EXT4_RESIZE_REPEATS:-3}"
fault_mode="${EXT4_RESIZE_FAULT:-7}"
run_dir="${work_dir}/ext4-resize-failure-$(date +%Y%m%d%H%M%S)-$$"
program="${run_dir}/ext4-resize-failure"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
active_pid_file=""

mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-resize-failure.c"
if [[ ! -e "${test_base_image}" ]]; then
	qemu-img create -f raw "${test_base_image}" 4G >/dev/null
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 32768 \
		-E lazy_itable_init=0 \
		"${test_base_image}" 500000
fi
if ! dumpe2fs -h "${test_base_image}" 2>/dev/null | \
		rg -q 'Block count:[[:space:]]+500000'; then
	echo "${test_base_image} must contain a 500000-block ext4 filesystem" >&2
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
	local recovered="${case_dir}/recovered.raw"
	local pid_file="${case_dir}/qemu.pid"
	local serial_log="${case_dir}/serial.log"
	local output="${case_dir}/program.txt"
	local dmesg="${case_dir}/dmesg.txt"
	local ssh_output
	local journal_rc fsck_rc

	mkdir -p "${case_dir}"
	active_pid_file="${pid_file}"
	qemu-img create -f qcow2 -F raw -b "${base_image}" "${root_overlay}" >/dev/null
	qemu-img create -f qcow2 -F raw -b "${test_base_image}" "${test_overlay}" >/dev/null
	qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
		-kernel "${EXT4_FAULT_KERNEL}" \
		-drive "file=${root_overlay},format=qcow2,if=virtio" \
		-drive "file=${test_overlay},format=qcow2,if=virtio" \
		-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
		-device virtio-net-pci,netdev=n0 \
		-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096" \
		-display none -monitor none -serial "file:${serial_log}" -daemonize \
		-pidfile "${pid_file}"

	for _ in $(seq 1 120); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'mkdir -p /mnt/ext4-resize; mountpoint -q /mnt/ext4-resize || mount /dev/vdb /mnt/ext4-resize; test -r /sys/module/ext4/parameters/ext4_fault_mode' \
			>/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mountpoint -q /mnt/ext4-resize && test -r /sys/module/ext4/parameters/ext4_fault_mode'
	scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		"printf ${fault} > /sys/module/ext4/parameters/ext4_fault_mode"
	ssh_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
		"/tmp/ext4-resize-failure /mnt/ext4-resize ${target_blocks}")"
	printf '%s\n' "${ssh_output}" > "${output}"
	ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg > "${dmesg}" 2>&1 || true
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'sync; umount /mnt/ext4-resize; systemctl poweroff' >/dev/null 2>&1 || true
	for _ in $(seq 1 60); do
		if [[ -s "${pid_file}" ]] && ! kill -0 "$(<"${pid_file}")" 2>/dev/null; then
			break
		fi
		sleep 1
	done
	if [[ -s "${pid_file}" ]] && kill -0 "$(<"${pid_file}")" 2>/dev/null; then
		echo 'guest did not stop cleanly' > "${case_dir}/e2fsck.txt"
		fsck_rc=125
	else
		qemu-img convert -f qcow2 -O raw "${test_overlay}" "${recovered}"
		set +e
		e2fsck -E journal_only -y "${recovered}" > "${case_dir}/journal-replay.txt" 2>&1
		journal_rc=$?
		e2fsck -fn "${recovered}" > "${case_dir}/e2fsck.txt" 2>&1
		fsck_rc=$?
		set -e
	fi
	{
		printf '[%s] fault=%s fsck_rc=%s journal_rc=%s\n' \
			"${label}" "${fault}" "${fsck_rc}" "${journal_rc:-unknown}"
		cat "${output}"
		rg 'resize group block addition failed|EXT4-fs.*error|WARNING:|BUG:|Oops' \
			"${dmesg}" || true
		tail -15 "${case_dir}/e2fsck.txt"
	} | tee "${case_dir}/report.txt"
	active_pid_file=""
}

run_case control 0
for n in $(seq 1 "${repeat}"); do
	run_case "fault-${fault_mode}-${n}" "${fault_mode}"
done
echo "resize failure test complete: ${run_dir}"
