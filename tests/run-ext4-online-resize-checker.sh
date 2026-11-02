#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
base_image="${SYZ_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${SYZ_PORT:-49331}"
run_dir="${work_dir}/ext4-online-resize-$(date +%Y%m%d%H%M%S)-$$"
disk="${run_dir}/disk.raw"
pid_file="${run_dir}/qemu.pid"
serial_log="${run_dir}/serial.log"
resize_blocks="${SYZ_RESIZE_BLOCKS:-786432}"
ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

mkdir -p "${run_dir}"
cp --reflink=auto "${base_image}" "${disk}"
truncate -s 4G "${disk}"
gcc -O2 -Wall -Wextra -static -o "${run_dir}/ext4-online-resize-checker" \
	"${root_dir}/tests/ext4-online-resize-checker.c"

cleanup() {
	if [[ -s "${pid_file}" ]]; then
		kill "$(<"${pid_file}")" 2>/dev/null || true
	fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage" \
	-drive "file=${disk},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096" \
	-display none -monitor none -serial "file:${serial_log}" -daemonize \
	-pidfile "${pid_file}"

for _ in $(seq 1 90); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'test -r /sys/kernel/debug/ext4/vda/invariant_check' >/dev/null 2>&1; then
		break
	fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test -r /sys/kernel/debug/ext4/vda/invariant_check'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/ext4-online-resize-checker" root@127.0.0.1:/root/

set +e
resize_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"/root/ext4-online-resize-checker / ${resize_blocks}")"
resize_rc=$?
summary_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'cat /sys/kernel/debug/ext4/vda/invariant_summary')"
summary_rc=$?
dmesg_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'dmesg | egrep -i "KASAN:|BUG:|WARNING:|Oops|EXT4-fs.*error|general protection|lockup|hung task" | tail -80 || true')"
dmesg_rc=$?
set -e
{
	printf '%s\n' "resize_rc=${resize_rc}" "${resize_output}"
	printf '%s\n' "summary_rc=${summary_rc}" "${summary_output}"
	printf '%s\n' "dmesg_rc=${dmesg_rc}" "${dmesg_output}"
} | tee "${run_dir}/report.txt"

cp "${serial_log}" "${run_dir}/serial.log.final" 2>/dev/null || true
echo "online resize checker probe complete: ${run_dir}"
