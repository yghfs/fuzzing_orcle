#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ext4-fault-env.sh"

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
fc_base="${EXT4_FC_BASE_IMAGE:-/tmp/ext4-fastcommit-base.raw}"
port="${EXT4_FC_REPLAY_PORT:-11552}"
run_dir="${work_dir}/ext4-fast-commit-inode-replay-$(date +%Y%m%d%H%M%S)-$$"
program="${run_dir}/ext4-fast-commit-inode-replay"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
active_pid=""

mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-fast-commit-inode-replay.c"
if [[ ! -e "${fc_base}" ]]; then
	qemu-img create -q -f raw "${fc_base}" 1G
	mke2fs -q -F -t ext4 -O fast_commit,^orphan_file -b 4096 -N 32768 \
		-E lazy_itable_init=0 -J size=4 "${fc_base}" 262144
fi
if ! dumpe2fs -h "${fc_base}" 2>/dev/null | rg -q 'fast_commit'; then
	echo "${fc_base} must have the fast_commit feature" >&2
	exit 2
fi
e2fsck -fn "${fc_base}" > "${run_dir}/baseline-fsck.txt" 2>&1 || true

cleanup() {
	if [[ -n "${active_pid}" ]]; then
		kill "${active_pid}" 2>/dev/null || true
	fi
}
trap cleanup EXIT

run_case() {
	local label="$1"
	local fault="$2"
	local case_dir="${run_dir}/${label}"
	local root_overlay="${case_dir}/root.qcow2"
	local test_overlay="${case_dir}/test.qcow2"
	local phase1_pid="${case_dir}/phase1.pid"
	local phase2_pid="${case_dir}/phase2.pid"
	local recovered="/tmp/ext4-fc-inode-${label}-$$.raw"
	local mount_output="${case_dir}/mount-output.txt"
	local verify_rc=125 journal_rc=125 fsck_rc=125
	local mounted=0

	mkdir -p "${case_dir}"
	qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${root_overlay}"
	qemu-img create -q -f qcow2 -F raw -b "${fc_base}" "${test_overlay}"
	qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
		-kernel "${EXT4_FAULT_KERNEL}" \
		-drive "file=${root_overlay},format=qcow2,if=virtio" \
		-drive "file=${test_overlay},format=qcow2,if=virtio" \
		-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
		-device virtio-net-pci,netdev=n0 \
		-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096 ext4.ext4_fault_mode=0" \
		-display none -monitor none -serial "file:${case_dir}/phase1.serial.log" \
		-daemonize -pidfile "${phase1_pid}"
	active_pid="$(<"${phase1_pid}")"
	for _ in $(seq 1 120); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'mkdir -p /mnt/ext4-fastcommit; mountpoint -q /mnt/ext4-fastcommit || mount -o commit=60 /dev/vdb /mnt/ext4-fastcommit; test -r /sys/module/ext4/parameters/ext4_fault_mode' \
			>/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mountpoint -q /mnt/ext4-fastcommit && printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
	scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null "${program}" \
		root@127.0.0.1:/tmp/ext4-fast-commit-inode-replay
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'nohup /tmp/ext4-fast-commit-inode-replay seed /mnt/ext4-fastcommit/fc-test > /tmp/ext4-fc-inode-seed.log 2>&1 < /dev/null &' 
	for _ in $(seq 1 60); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'grep -q fast_commit_ready /tmp/ext4-fc-inode-seed.log' \
			>/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'grep -q fast_commit_ready /tmp/ext4-fc-inode-seed.log'
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'cat /tmp/ext4-fc-inode-seed.log' > "${case_dir}/seed.txt"
	kill -9 "${active_pid}" 2>/dev/null || true
	active_pid=""
	for _ in $(seq 1 20); do
		if ! kill -0 "$(<"${phase1_pid}")" 2>/dev/null; then
			break
		fi
		sleep 1
	done

	qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
		-kernel "${EXT4_FAULT_KERNEL}" \
		-drive "file=${root_overlay},format=qcow2,if=virtio" \
		-drive "file=${test_overlay},format=qcow2,if=virtio" \
		-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
		-device virtio-net-pci,netdev=n0 \
		-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096 ext4.ext4_fault_mode=${fault}" \
		-display none -monitor none -serial "file:${case_dir}/phase2.serial.log" \
		-daemonize -pidfile "${phase2_pid}"
	active_pid="$(<"${phase2_pid}")"
	for _ in $(seq 1 120); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'mkdir -p /mnt/ext4-fastcommit; mountpoint -q /mnt/ext4-fastcommit || mount -o commit=60 /dev/vdb /mnt/ext4-fastcommit; test -r /sys/kernel/debug/ext4/vdb/invariant_check' \
			>/dev/null 2>&1; then
			mounted=1
			break
		fi
		sleep 1
	done
	if [[ "${mounted}" == 1 ]]; then
		scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
			-o UserKnownHostsFile=/dev/null "${program}" \
			root@127.0.0.1:/tmp/ext4-fast-commit-inode-replay
		set +e
		ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'/tmp/ext4-fast-commit-inode-replay verify /mnt/ext4-fastcommit/fc-test; rc=$?; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | tail -100; sync; umount /mnt/ext4-fastcommit; systemctl poweroff; exit $rc' \
			> "${mount_output}" 2>&1
		verify_rc=$?
		set -e
	fi
	if [[ -s "${phase2_pid}" ]]; then
		for _ in $(seq 1 45); do
			if ! kill -0 "$(<"${phase2_pid}")" 2>/dev/null; then
				break
			fi
			sleep 1
		done
		if kill -0 "$(<"${phase2_pid}")" 2>/dev/null; then
			kill -9 "${active_pid}" 2>/dev/null || true
		fi
	fi
	active_pid=""
	qemu-img convert -q -f qcow2 -O raw "${test_overlay}" "${recovered}"
	set +e
	e2fsck -E journal_only -y "${recovered}" \
		> "${case_dir}/journal-replay.txt" 2>&1
	journal_rc=$?
	e2fsck -fn "${recovered}" > "${case_dir}/e2fsck.txt" 2>&1
	fsck_rc=$?
	set -e
	ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg \
		> "${case_dir}/dmesg.txt" 2>/dev/null || true
	{
		printf '[%s] fault=%s mounted=%s verify_rc=%s journal_rc=%s fsck_rc=%s\n' \
			"${label}" "${fault}" "${mounted}" "${verify_rc}" \
			"${journal_rc}" "${fsck_rc}"
		cat "${case_dir}/seed.txt" "${mount_output}"
		rg 'fast-commit inode tracking failed|fast commit replay|EXT4-fs.*error|BUG:|Oops' \
			"${case_dir}/phase2.serial.log" "${case_dir}/dmesg.txt" || true
		tail -15 "${case_dir}/e2fsck.txt"
	} | tee "${case_dir}/report.txt"
}

run_case control 0
run_case fail-record 9
printf 'run_dir=%s\n' "${run_dir}"
