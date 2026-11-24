#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${root_dir}/tests/ext4-normal-env.sh"
work_dir="${root_dir}/work"
kernel_image="${FUZZING_ORCLE_BZIMAGE}"
base_image="${SYZ_BASE_IMAGE:-${ext4_normal_guest_image}}"
syz_bin="${FUZZING_ORCLE_SYZKALLER:-${root_dir}/tools/syzkaller}/bin/linux_amd64"
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

ext4_require_normal_kernel "${kernel_image}"
test -r "${workload}" || { echo "missing workload: ${workload}" >&2; exit 2; }
test -x "${syz_bin}/syz-execprog" && test -x "${syz_bin}/syz-executor" || {
	echo "missing syzkaller replay tools in ${syz_bin}; run tools/build-syzkaller.sh" >&2
	exit 2
}
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
printf 'experiment=runtime-checker\n' > "${run_dir}/experiment.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"cat /sys/kernel/debug/ext4/vda/invariant_check | sed -n '1,5p'" \
	| tee "${run_dir}/before.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"printf 1 > /sys/kernel/debug/ext4/vda/invariant_reset"

scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${workload}" "${syz_bin}/syz-execprog" "${syz_bin}/syz-executor" root@127.0.0.1:/tmp/
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
exit \$rc
" | tee "${run_dir}/report.txt"

rg -qx 'violations_total=0' "${run_dir}/report.txt"
rg -q '^tx_underflows=0$' "${run_dir}/report.txt"
echo "checker workload complete: ${run_dir}"
