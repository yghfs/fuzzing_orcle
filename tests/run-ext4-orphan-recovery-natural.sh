#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_ORPHAN_RECOVERY_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
kernel_config="${EXT4_ORPHAN_RECOVERY_CONFIG:-$(dirname "${kernel}")/config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_ORPHAN_RECOVERY_BASE:-${work_dir}/ext4-orphan-recovery-256m.raw}"
port="${EXT4_ORPHAN_RECOVERY_PORT:-11574}"
run_dir="${EXT4_ORPHAN_RECOVERY_RUN_DIR:-${work_dir}/ext4-orphan-recovery-natural-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
active_pid=""

if [[ ! -r "${kernel}" || ! -r "${kernel_config}" || ! -r "${root_base}" ]]; then
	echo "missing kernel, config, or root image" >&2
	exit 2
fi
if ! grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${kernel_config}" ||
	grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
	echo "test requires the checker enabled and fault injection disabled" >&2
	exit 2
fi

mkdir -p "${run_dir}"
program="${run_dir}/ext4-orphan-recovery-natural"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-orphan-recovery-natural.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 256M
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 16384 \
		-E lazy_itable_init=0 -J size=4 "${test_base}"
fi
if dumpe2fs -h "${test_base}" 2>/dev/null | rg -q 'Filesystem features:.*orphan_file'; then
	echo "test image must use the traditional orphan list" >&2
	exit 2
fi
e2fsck -fn "${test_base}" > "${run_dir}/baseline-fsck.txt" 2>&1 || true

root_overlay="${run_dir}/root.qcow2"
test_overlay="${run_dir}/test.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${root_overlay}"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${test_overlay}"

cleanup() {
	if [[ -n "${active_pid}" ]]; then
		kill "${active_pid}" 2>/dev/null || true
		wait "${active_pid}" 2>/dev/null || true
	fi
}
trap cleanup EXIT

boot_guest() {
	local phase="$1"
	local serial="${run_dir}/serial-${phase}.log"
	local pid_file="${run_dir}/qemu-${phase}.pid"
	local started=0
	for _ in $(seq 1 10); do
		if qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
			-kernel "${kernel}" \
			-drive "file=${root_overlay},format=qcow2,if=virtio" \
			-drive "file=${test_overlay},format=qcow2,if=virtio" \
			-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
			-device virtio-net-pci,netdev=n0 \
			-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=16384" \
			-display none -monitor none -serial "file:${serial}" \
			-daemonize -pidfile "${pid_file}" 2>"${run_dir}/qemu-${phase}.start.log"; then
			started=1
			break
		fi
		sleep 1
	done
	if [[ "${started}" != 1 ]]; then
		cat "${run_dir}/qemu-${phase}.start.log" >&2
		return 1
	fi
	active_pid="$(<"${pid_file}")"
	for _ in $(seq 1 120); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'mkdir -p /mnt/test; mountpoint -q /mnt/test || mount -o commit=1 /dev/vdb /mnt/test; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
			>/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	tail -80 "${serial}" >&2
	return 1
}

boot_guest prepare
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" \
	root@127.0.0.1:/tmp/ext4-orphan-recovery-natural
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'nohup /tmp/ext4-orphan-recovery-natural prepare /mnt/test/orphan-case > /tmp/orphan-prepare.log 2>&1 < /dev/null &'
ready=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'grep -q orphan_prepared /tmp/orphan-prepare.log' >/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 1
done
if [[ "${ready}" != 1 ]]; then
	ssh "${ssh_opts[@]}" root@127.0.0.1 'cat /tmp/orphan-prepare.log; dmesg | tail -80' \
		> "${run_dir}/prepare-failure.txt" 2>&1 || true
	cat "${run_dir}/prepare-failure.txt" >&2
	exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'cat /tmp/orphan-prepare.log; sync; cat /sys/kernel/debug/ext4/vdb/invariant_summary' \
	> "${run_dir}/before-crash.txt"
kill -9 "${active_pid}" 2>/dev/null || true
for _ in $(seq 1 20); do
	kill -0 "${active_pid}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${active_pid}" 2>/dev/null; then
	echo "first QEMU did not exit after power cut" >&2
	exit 1
fi
active_pid=""

boot_guest recover
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" \
	root@127.0.0.1:/tmp/ext4-orphan-recovery-natural
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'/tmp/ext4-orphan-recovery-natural verify /mnt/test/orphan-case; rc=$?; sync; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | tail -100; umount /mnt/test; systemctl poweroff; exit $rc' \
	> "${run_dir}/after-recovery.txt" 2>&1
verify_rc=$?
set -e
ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg \
	> "${run_dir}/dmesg-after-recovery.txt" 2>&1 || true
cleanup
active_pid=""

qemu-img convert -q -f qcow2 -O raw "${test_overlay}" "${run_dir}/recovered.raw"
set +e
e2fsck -E journal_only -y "${run_dir}/recovered.raw" \
	> "${run_dir}/journal-replay.txt" 2>&1
journal_rc=$?
e2fsck -fn "${run_dir}/recovered.raw" > "${run_dir}/e2fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'verify_rc=%s journal_rc=%s fsck_rc=%s\n' \
	"${verify_rc}" "${journal_rc}" "${fsck_rc}" > "${run_dir}/report.txt"
rg -n 'orphan_prepared|orphan_recovery_verify|snapshot OK|violations_total=|unverified_total=|snapshot_races=|EXT4-fs.*(error|warning)|Free .* count|bitmap differences|Filesystem still has errors' \
	"${run_dir}/before-crash.txt" "${run_dir}/after-recovery.txt" \
	"${run_dir}/dmesg-after-recovery.txt" "${run_dir}/e2fsck.txt" \
	>> "${run_dir}/report.txt" || true
cat "${run_dir}/report.txt"
printf 'run_dir=%s\n' "${run_dir}"
if [[ "${verify_rc}" -ne 0 || "${journal_rc}" -gt 1 || "${fsck_rc}" -ne 0 ]] ||
	rg -q 'violations_total=[1-9]' "${run_dir}/after-recovery.txt"; then
	exit 1
fi
