#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage"
disk_image="${work_dir}/debian.img"
ssh_key="${work_dir}/debian.id_rsa"
port="${1:-10050}"
pid_file="${work_dir}/ext4-syz-${port}.pid"
serial_log="${work_dir}/ext4-syz-${port}.log"
syz_repeat="${SYZ_REPEAT:-10}"
syz_procs="${SYZ_PROCS:-2}"
syz_threaded="${SYZ_THREADED:-1}"
syz_timeout="${SYZ_TIMEOUT:-90}"
syz_debug="${SYZ_DEBUG:-0}"
checker_event_cap="${CHECKER_EVENT_CAP:-8192}"
vm_wait="${SYZ_VM_WAIT:-120}"
ready=0

if ! [[ "${vm_wait}" =~ ^[1-9][0-9]*$ ]]; then
	echo "ext4 workload: SYZ_VM_WAIT must be a positive integer (got: ${vm_wait})" >&2
	exit 2
fi

cleanup() {
	if [[ -s "${pid_file}" ]]; then
		kill "$(<"${pid_file}")" 2>/dev/null || true
	fi
}
trap cleanup EXIT

qemu-system-x86_64 \
	-machine q35,accel=tcg -cpu max -m 1024 -smp 2 \
	-kernel "${kernel_image}" \
	-drive "file=${disk_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=${checker_event_cap}" \
	-display none -monitor none -serial "file:${serial_log}" -daemonize \
	-pidfile "${pid_file}"

ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o NumberOfPasswordPrompts=0 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
scp_opts=(-i "${ssh_key}" -P "${port}" -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o NumberOfPasswordPrompts=0 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

# Wait for the guest in two stages.  SSH can become reachable before systemd
# has mounted debugfs, and the old single probe treated that normal delay as a
# boot failure.
ssh_ready=0
last_ssh_status=255
last_ssh_output=""
for _ in $(seq 1 "${vm_wait}"); do
	if ssh_probe="$(ssh -o ConnectTimeout=2 "${ssh_opts[@]}" root@127.0.0.1 'true' 2>&1)"; then
		ssh_ready=1
		break
	else
		last_ssh_status=$?
		last_ssh_output="${ssh_probe}"
	fi
	sleep 1
done
if [[ "${ssh_ready}" != 1 ]]; then
	echo "ext4 workload: SSH did not become ready after ${vm_wait}s" >&2
	echo "ext4 workload: last SSH probe status=${last_ssh_status}${last_ssh_output:+ output=${last_ssh_output}}" >&2
	cat "${serial_log}" >&2 || true
	exit 1
fi

last_checker_status=255
last_checker_output=""
for _ in $(seq 1 "${vm_wait}"); do
	if checker_probe="$(ssh -o ConnectTimeout=2 "${ssh_opts[@]}" root@127.0.0.1 \
		'mountpoint -q /sys/kernel/debug || mount -t debugfs debugfs /sys/kernel/debug || { echo debugfs_mount_failed; exit 10; }; [ -d /sys/kernel/debug/ext4 ] || { echo ext4_debugfs_unavailable; exit 11; }; checker_path=$(find /sys/kernel/debug/ext4 -type f -name invariant_check -print -quit 2>/dev/null); [ -n "$checker_path" ] || { echo checker_not_exposed; exit 12; }; printf "%s\\n" "$checker_path"' \
		2>&1)"; then
		last_checker_status=0
		checker_path="${checker_probe}"
	else
		last_checker_status=$?
		last_checker_output="${checker_probe}"
		checker_path=""
	fi
	if [[ -n "${checker_path}" ]]; then
		ready=1
		break
	fi
	sleep 1
done
if [[ "${ready:-0}" != 1 ]]; then
	echo "ext4 workload: checker was not exposed after SSH became ready (${vm_wait}s)" >&2
	echo "ext4 workload: last checker probe status=${last_checker_status}${last_checker_output:+ output=${last_checker_output}}" >&2
	cat "${serial_log}" >&2 || true
	exit 1
fi

scp "${scp_opts[@]}" "${root_dir}/tests/ext4-workload.syz" root@127.0.0.1:/tmp/ext4-workload.syz >/dev/null
scp "${scp_opts[@]}" \
	"${root_dir}/tools/syzkaller/bin/linux_amd64/syz-execprog" \
	"${root_dir}/tools/syzkaller/bin/linux_amd64/syz-executor" \
	root@127.0.0.1:/tmp/ >/dev/null

ssh "${ssh_opts[@]}" root@127.0.0.1 '
set -eu
mountpoint -q /sys/kernel/debug || mount -t debugfs debugfs /sys/kernel/debug
test -d /sys/kernel/debug/ext4
rm -rf /tmp/ext4-syzkaller
mkdir -p /tmp/ext4-syzkaller
cd /tmp/ext4-syzkaller
chmod +x /tmp/syz-execprog /tmp/syz-executor
exec_args="-executor=/tmp/syz-executor -sandbox=none -threaded='"${syz_threaded}"' -repeat='"${syz_repeat}"' -procs='"${syz_procs}"'"
if [ "'"${syz_debug}"'" = 1 ]; then
	set -x
fi
timeout --foreground '"${syz_timeout}"' sh -c "/tmp/syz-execprog ${exec_args} /tmp/ext4-workload.syz" > /tmp/syz-execprog.log 2>&1 || exec_rc=$?
exec_rc=${exec_rc:-0}
cat /tmp/syz-execprog.log
printf "syz-execprog exit=%s repeat=%s procs=%s threaded=%s\n" "$exec_rc" '"${syz_repeat}"' '"${syz_procs}"' '"${syz_threaded}"'
checker_path=$(find /sys/kernel/debug/ext4 -type f -name invariant_check -print -quit 2>/dev/null)
test -n "$checker_path"
report=$(cat "$checker_path")
printf "%s\n" "$report"
if [ "$exec_rc" -ne 0 ] && [ "$exec_rc" -ne 124 ]; then
	echo "syz-execprog failed; checker report follows" >&2
fi
printf '%s\n' "$report" | grep -q "snapshot OK"
printf '%s\n' "$report" | grep -q "type=inode_alloc"
printf '%s\n' "$report" | grep -q "type=cluster_alloc"
	printf '%s\n' "$report" | grep -q "type=tx_begin"
printf '%s\n' "$report" | grep -q "type=tx_end"
printf '%s\n' "$report" | grep -q "snapshots=.*events_dropped=0"
'
remote_rc=${PIPESTATUS[0]}
if [[ "${remote_rc}" -ne 0 ]]; then
	echo "syzkaller ext4 workload: remote runner failed" >&2
	cat "${serial_log}" >&2 || true
	exit 1
fi

echo "syzkaller ext4 workload: detected detailed events"
