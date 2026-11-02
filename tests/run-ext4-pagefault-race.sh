#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${EXT4_RACE_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_RACE_PORT:-11231}"
iterations="${EXT4_RACE_ITERATIONS:-1000}"
mode="${EXT4_RACE_MODE:-all}"
source_file="${EXT4_RACE_SOURCE:-${root_dir}/tests/ext4-pagefault-unwritten-race.c}"
program_name="${EXT4_RACE_NAME:-ext4-pagefault-unwritten-race}"
program_args="${EXT4_RACE_ARGS-${iterations} ${mode}}"
completion_pattern="${EXT4_RACE_COMPLETE_PATTERN:-iterations=.*mmap_writes=}"
interval="${EXT4_RACE_INTERVAL:-2}"
max_samples="${EXT4_RACE_MAX_SAMPLES:-300}"
event_cap="${EXT4_RACE_EVENT_CAP:-65536}"
run_dir="${work_dir}/ext4-pagefault-race-$(date +%Y%m%d%H%M%S)-$$"
overlay="${run_dir}/disk.qcow2"
recovered_image="${EXT4_RACE_RECOVERED_IMAGE:-${run_dir}/recovered.raw}"
pid_file="${run_dir}/qemu.pid"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=3
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

mkdir -p "${run_dir}"
gcc -static -O2 -Wall -Wextra -pthread \
	"${source_file}" -o "${run_dir}/${program_name}"
qemu-img create -f qcow2 -F raw -b "${base_image}" "${overlay}" >/dev/null
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024 -smp 2 \
	-kernel "${kernel_image}" -drive "file=${overlay},format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=${event_cap}" \
	-display none -monitor none -serial "file:${run_dir}/serial.log" -daemonize \
	-pidfile "${pid_file}"
qemu_pid="$(<"${pid_file}")"
cleanup() {
	if [[ -s "${pid_file}" ]]; then kill "$(<"${pid_file}")" 2>/dev/null || true; fi
}
trap cleanup EXIT

checker_path='/sys/kernel/debug/ext4/vda/invariant_check'
for _ in $(seq 1 180); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 "test -r ${checker_path}" >/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 "test -r ${checker_path}"
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	"${run_dir}/${program_name}" \
	"root@127.0.0.1:/tmp/${program_name}"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"printf 1 > /sys/kernel/debug/ext4/vda/invariant_reset"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"cat ${checker_path}" > "${run_dir}/before.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"chmod +x /tmp/${program_name}"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"/tmp/${program_name} ${program_args} \
	> /tmp/${program_name}.log 2>&1" \
	> "${run_dir}/workload-ssh.log" 2>&1 &
workload_ssh_pid=$!

for sample in $(seq 1 "${max_samples}"); do
	if ! kill -0 "${workload_ssh_pid}" 2>/dev/null; then
		break
	fi
	{
		printf 'sample=%s\n' "${sample}"
		ssh "${ssh_opts[@]}" root@127.0.0.1 "head -n 12 ${checker_path}" 2>&1 || true
	} >> "${run_dir}/timeline.log"
	ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg > "${run_dir}/dmesg-live.txt" 2>&1 || true
	sleep "${interval}"
done
set +e
wait "${workload_ssh_pid}"
workload_rc=$?
set -e
printf 'ssh_workload_exit=%s\n' "${workload_rc}" >> "${run_dir}/workload-ssh.log"

ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"cat /tmp/${program_name}.log" \
	> "${run_dir}/program.txt" 2>&1 || true
workload_ok=0
if rg -q "${completion_pattern}" "${run_dir}/program.txt"; then
	workload_ok=1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"cat ${checker_path}" > "${run_dir}/checker-after-run.txt" 2>&1 || true
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vda/invariant_reset' >/dev/null 2>&1 || true
for quiet_sample in 1 2 3; do
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		"cat ${checker_path}" > "${run_dir}/quiet-${quiet_sample}.txt" 2>&1 || true
	sleep 1
done
cp "${run_dir}/quiet-3.txt" "${run_dir}/checker-final.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg > "${run_dir}/dmesg-final.txt" 2>&1 || true
ssh "${ssh_opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 30); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then break; fi
	sleep 1
done
kill "${qemu_pid}" 2>/dev/null || true
qemu-img convert -f qcow2 -O raw "${overlay}" "${recovered_image}"
set +e
e2fsck -fn "${recovered_image}" > "${run_dir}/e2fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'e2fsck_exit=%s\n' "${fsck_rc}" >> "${run_dir}/e2fsck.txt"
rg -n '^(snapshot|violations_total|last_violation|unverified_total|snapshot_races|snapshots|tx_underflows|events|tx_context)|EXT4-fs error|BUG:|Oops:|KASAN:|JBD2:|EXT4-fs warning' \
	"${run_dir}/checker-after-run.txt" "${run_dir}/checker-final.txt" "${run_dir}/dmesg-final.txt" \
	"${run_dir}/serial.log" "${run_dir}/e2fsck.txt" \
	> "${run_dir}/summary.txt" || true
printf 'run_dir=%s\n' "${run_dir}"
if [[ "${workload_ok}" != 1 ]]; then
	echo "workload did not produce a completion record; inspect ${run_dir}/program.txt" >&2
	exit 1
fi
