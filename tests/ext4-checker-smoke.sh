#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage"
base_image="${SYZ_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
ssh_key="${work_dir}/debian.id_rsa"
port="${1:-10030}"
run_id="${port}-$$"
overlay="${work_dir}/checker-smoke-${run_id}.qcow2"
pid_file="${work_dir}/checker-smoke-${run_id}.pid"
serial_log="${work_dir}/checker-smoke-${run_id}.log"

cleanup() {
	if [[ -s "${pid_file}" ]]; then
		kill "$(<"${pid_file}")" 2>/dev/null || true
		echo "$(<"${pid_file}")" > "${pid_file}.stopped"
	fi
	rm -f "${overlay}"
}
trap cleanup EXIT

qemu-img create -f qcow2 -F raw -b "${base_image}" "${overlay}" >/dev/null

qemu-system-x86_64 \
	-machine q35,accel=tcg -cpu max -m 1024 -smp 2 \
	-kernel "${kernel_image}" \
	-drive "file=${overlay},format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096" \
	-display none -monitor none -serial "file:${serial_log}" -daemonize \
	-pidfile "${pid_file}"

for _ in $(seq 1 40); do
	if ssh -i "${ssh_key}" -p "${port}" -o ConnectTimeout=1 \
		-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1 \
		'test -r /sys/kernel/debug/ext4/vda/invariant_check' >/dev/null 2>&1; then
		break
	fi
	sleep 1
done

ssh_opts=(-i "${ssh_key}" -p "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test ! -e /sys/module/ext4/parameters/ext4_fault_mode'

report="$(ssh "${ssh_opts[@]}" root@127.0.0.1 '
set -eu
test_dir=/tmp/ext4-checker-smoke
rm -rf "$test_dir"
mkdir -p "$test_dir/subdir"
dd if=/dev/zero of="$test_dir/file" bs=4096 count=16 status=none
truncate -s 0 "$test_dir/file"
rm -rf "$test_dir"
cat /sys/kernel/debug/ext4/vda/invariant_check
')"
echo "${report}"

rg -q 'snapshot OK' <<<"${report}"
rg -q '^violations_total=0$' <<<"${report}"

echo "runtime checker smoke: passed (fault injection disabled)"
