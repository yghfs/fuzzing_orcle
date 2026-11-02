#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_ORPHAN_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
kernel_config="${EXT4_ORPHAN_CONFIG:-}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_ORPHAN_BASE:-${work_dir}/ext4-orphan-file-natural-1g-65536.raw}"
port="${EXT4_ORPHAN_PORT:-11691}"
workers="${EXT4_ORPHAN_WORKERS:-8}"
iterations="${EXT4_ORPHAN_ITERATIONS:-1000}"
mode="${EXT4_ORPHAN_MODE:-churn}"
hold_seconds="${EXT4_ORPHAN_HOLD_SECONDS:-30}"
run_dir="${EXT4_ORPHAN_RUN_DIR:-${work_dir}/ext4-orphan-file-natural-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
if [[ -z "${kernel_config}" && "${kernel}" == */arch/x86/boot/bzImage ]]; then
	kernel_config="${kernel%/arch/x86/boot/bzImage}/.config"
fi
if [[ -r "${kernel_config}" ]]; then
	config_text="$(<"${kernel_config}")"
elif config_text="$("${root_dir}/kernel/linux-6.12.1/scripts/extract-ikconfig" \
	"${kernel}" 2>/dev/null)"; then
	:
else
	config_text=""
fi
if [[ -n "${config_text}" ]] && {
	! grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' <<<"${config_text}" ||
	! grep -Eq '^# CONFIG_EXT4_FAULT_INJECTION is not set$' <<<"${config_text}";
}; then
	echo "selected kernel config must enable the checker and disable fault injection" >&2
	exit 2
elif [[ -z "${config_text}" ]]; then
	echo "selected kernel config unavailable; guest runtime checks will verify checker/fault controls" >&2
fi
if [[ ! "${workers}" =~ ^[0-9]+$ || ! "${iterations}" =~ ^[0-9]+$ ||
	! "${hold_seconds}" =~ ^[0-9]+$ ||
	"${hold_seconds}" -gt 300 ||
	( "${mode}" != churn && "${mode}" != hold ) ]]; then
	echo "workers, iterations, and hold seconds must be integers; mode must be churn or hold" >&2
	exit 2
fi
mkdir -p "${run_dir}"
program="${run_dir}/ext4-orphan-file-count-stress"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" "${root_dir}/tests/ext4-orphan-file-count-stress.c"
if [[ ! -r "${kernel}" || ! -r "${root_base}" ]]; then
	echo "missing kernel or root image" >&2
	exit 2
fi
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mke2fs -q -F -t ext4 -O orphan_file -b 4096 -N 65536 -E lazy_itable_init=0 "${test_base}"
fi
if ! dumpe2fs -h "${test_base}" 2>/dev/null | grep -q 'Filesystem features:.*orphan_file'; then
	echo "test image does not have the ext4 orphan_file feature: ${test_base}" >&2
	exit 2
fi
root_overlay="${run_dir}/root.qcow2"
test_overlay="${run_dir}/test.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${root_overlay}"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${test_overlay}"
qemu_pid="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${root_overlay},format=qcow2,if=virtio" \
	-drive "file=${test_overlay},format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" -device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" -daemonize -pidfile "${qemu_pid}"
qemu_pid_value="$(<"${qemu_pid}")"
cleanup() { [[ ! -s "${qemu_pid}" ]] || kill "$(<"${qemu_pid}")" 2>/dev/null || true; }
trap cleanup EXIT
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 'mkdir -p /mnt/ext4-orphan; mountpoint -q /mnt/ext4-orphan || mount /dev/vdb /mnt/ext4-orphan; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1; then break; fi
	sleep 1
done
ssh "${opts[@]}" root@127.0.0.1 'test ! -e /sys/module/ext4/parameters/ext4_fault_mode && mount -o remount,commit=1 /mnt/ext4-orphan'
program="${run_dir}/ext4-orphan-file-count-stress"
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/ext4-orphan-file-count-stress
ssh "${opts[@]}" root@127.0.0.1 "rm -f /tmp/ext4-orphan-hold-ready; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; nohup /tmp/ext4-orphan-file-count-stress /mnt/ext4-orphan/workload ${workers} ${iterations} ${mode} ${hold_seconds} >/tmp/ext4-orphan-file-count-stress.log 2>&1 </dev/null & echo \$! >/tmp/ext4-orphan-file-count-stress.pid"
if [[ "${mode}" == hold ]]; then
	ready=0
	for _ in $(seq 1 600); do
		if ssh "${opts[@]}" root@127.0.0.1 'test -s /tmp/ext4-orphan-hold-ready' >/dev/null 2>&1; then
			ready=1
			break
		fi
		ssh "${opts[@]}" root@127.0.0.1 'p=$(cat /tmp/ext4-orphan-file-count-stress.pid); kill -0 "$p" 2>/dev/null' >/dev/null 2>&1 || break
		sleep 1
	done
	if [[ "${ready}" != 1 ]]; then
		ssh "${opts[@]}" root@127.0.0.1 'cat /tmp/ext4-orphan-file-count-stress.log; cat /tmp/ext4-orphan-hold-ready 2>/dev/null || true' || true
		echo 'orphan hold workers did not reach the ready state' >&2
		exit 1
	fi
	ssh "${opts[@]}" root@127.0.0.1 'cat /tmp/ext4-orphan-hold-ready' >"${run_dir}/hold-state.txt"
	ssh "${opts[@]}" root@127.0.0.1 'for i in 1 2 3; do echo "snapshot=$i"; cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary; sleep 1; done' >"${run_dir}/summary-during-hold.txt"
else
	for _ in $(seq 1 30); do
		ssh "${opts[@]}" root@127.0.0.1 'cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; p=$(cat /tmp/ext4-orphan-file-count-stress.pid); kill -0 "$p" 2>/dev/null || exit 1' >/dev/null 2>&1 || break
		sleep 0.5
	done
fi
ssh "${opts[@]}" root@127.0.0.1 'for i in $(seq 1 300); do p=$(cat /tmp/ext4-orphan-file-count-stress.pid); kill -0 "$p" 2>/dev/null || break; sleep 1; done; cat /tmp/ext4-orphan-file-count-stress.log; sync'
ssh "${opts[@]}" root@127.0.0.1 'cat /sys/kernel/debug/ext4/vdb/invariant_summary' >"${run_dir}/summary-before-quiet.txt"
for _ in 1 2 3; do ssh "${opts[@]}" root@127.0.0.1 'cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null'; done
ssh "${opts[@]}" root@127.0.0.1 'cat /sys/kernel/debug/ext4/vdb/invariant_summary' >"${run_dir}/summary-after-quiet.txt"
ssh "${opts[@]}" root@127.0.0.1 'dmesg | grep -E "orphan_file_entries|EXT4-fs.*error|BUG:|WARNING:" | tail -100 || true' >"${run_dir}/dmesg.txt"
ssh "${opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do kill -0 "${qemu_pid_value}" 2>/dev/null || break; sleep 1; done
if kill -0 "${qemu_pid_value}" 2>/dev/null; then
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${test_overlay}" "${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s fsck_rc=%s\n' "${run_dir}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'orphan_file_count_stress|orphan_worker|model_orphan_file_entries|violations_total|snapshot_count|unverified|held=' "${run_dir}/hold-state.txt" "${run_dir}/summary-during-hold.txt" "${run_dir}/summary-before-quiet.txt" "${run_dir}/summary-after-quiet.txt" "${run_dir}/dmesg.txt" | tee -a "${run_dir}/report.txt" || true
tail -20 "${run_dir}/fsck.txt"
