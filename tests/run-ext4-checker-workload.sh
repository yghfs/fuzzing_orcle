#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage"
base_image="${SYZ_BASE_IMAGE:-${work_dir}/debian.img}"
workload="${1:-${SYZ_WORKLOAD:-${root_dir}/tests/ext4-workload.syz}}"
name="$(basename "${workload}" .syz)"
port="${SYZ_PORT:-10100}"
run_dir="${work_dir}/checker-${name}-${port}-$(date +%Y%m%d%H%M%S)-$$"
overlay="${run_dir}/disk.qcow2"
pid_file="${run_dir}/qemu.pid"
serial_log="${run_dir}/serial.log"
repeat="${SYZ_REPEAT:-1}"
procs="${SYZ_PROCS:-1}"
threaded="${SYZ_THREADED:-0}"
timeout_s="${SYZ_TIMEOUT:-60}"
event_cap="${CHECKER_EVENT_CAP:-16384}"
vm_wait="${SYZ_VM_WAIT:-90}"

mkdir -p "${run_dir}"
qemu-img create -f qcow2 -F raw -b "${base_image}" "${overlay}" >/dev/null
cleanup() {
	if [[ -s "${pid_file}" ]]; then kill "$(<"${pid_file}")" 2>/dev/null || true; fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024 -smp 2 \
	-kernel "${kernel_image}" -drive "file=${overlay},format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=${event_cap}" \
	-display none -monitor none -serial "file:${serial_log}" -daemonize -pidfile "${pid_file}"

ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes -o ConnectTimeout=2 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
for _ in $(seq 1 "${vm_wait}"); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 'test -r /sys/kernel/debug/ext4/vda/invariant_check' >/dev/null 2>&1; then break; fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 'test -r /sys/kernel/debug/ext4/vda/invariant_check'
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode' || {
	echo "Runtime checking requires a kernel with CONFIG_EXT4_FAULT_INJECTION disabled." >&2
	exit 2
}
printf 'experiment=runtime-checker\nfault_injection=disabled\n' > "${run_dir}/experiment.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"cat /sys/kernel/debug/ext4/vda/invariant_check | sed -n '1,5p'" \
	| tee "${run_dir}/before.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"printf 1 > /sys/kernel/debug/ext4/vda/invariant_reset"

scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${workload}" "${root_dir}/tools/syzkaller/bin/linux_amd64/syz-execprog" \
	"${root_dir}/tools/syzkaller/bin/linux_amd64/syz-executor" root@127.0.0.1:/tmp/
remote_name="$(basename "${workload}")"
ssh "${ssh_opts[@]}" root@127.0.0.1 "
set -eu
chmod +x /tmp/syz-execprog /tmp/syz-executor
rm -rf /tmp/ext4-checker-run && mkdir /tmp/ext4-checker-run
cd /tmp/ext4-checker-run
if [ "${repeat}" = 1 ] && [ "${procs}" = 1 ]; then
	/tmp/syz-execprog -executor=/tmp/syz-executor -sandbox=none -threaded=${threaded} -repeat=1 -procs=1 /tmp/${remote_name} > /tmp/ext4-checker-run.log 2>&1 || rc=\$?
else
	timeout --foreground ${timeout_s} /tmp/syz-execprog -executor=/tmp/syz-executor -sandbox=none -threaded=${threaded} -repeat=${repeat} -procs=${procs} /tmp/${remote_name} > /tmp/ext4-checker-run.log 2>&1 || rc=\$?
fi
rc=\${rc:-0}
cat /tmp/ext4-checker-run.log
echo syz_execprog_exit=\$rc
cat /sys/kernel/debug/ext4/vda/invariant_check
" | tee "${run_dir}/report.txt"

echo "checker workload complete: ${run_dir}"
