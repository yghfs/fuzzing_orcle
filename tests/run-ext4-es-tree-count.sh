#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${ES_TREE_RUN_DIR:-${work_dir}/ext4-es-tree-$(date +%Y%m%d%H%M%S)-$$}"
kernel="${ES_TREE_KERNEL:-${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage}"
base_image="${ES_TREE_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
data_size="${ES_TREE_DISK_SIZE:-384M}"
port="${ES_TREE_PORT:-12165}"
event_cap="${ES_TREE_EVENT_CAP:-65536}"

mkdir -p "${run_dir}"
test -r "${kernel}" || { echo "missing kernel: ${kernel}" >&2; exit 2; }
test -r "${base_image}" || { echo "missing base image: ${base_image}" >&2; exit 2; }
gcc -O2 -Wall -Wextra -Werror -static \
	-o "${run_dir}/es-tree-count-workload" \
	"${root_dir}/tests/ext4-es-tree-count-workload.c"
qemu-img create -q -f qcow2 -F raw -b "${base_image}" "${run_dir}/root.qcow2"
qemu-img create -q -f raw "${run_dir}/data.raw" "${data_size}"
mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 8192 \
	-E lazy_itable_init=0 "${run_dir}/data.raw"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/data.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=${event_cap}" \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"

qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT
ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes \
	-o ConnectTimeout=3 -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/es-tree; mountpoint -q /mnt/es-tree || mount /dev/vdb /mnt/es-tree; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then break; fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test -r /sys/kernel/debug/ext4/vdb/invariant_summary && test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/es-tree-count-workload" root@127.0.0.1:/tmp/

ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"set -eu; mkdir -p /mnt/es-tree; mountpoint -q /mnt/es-tree || mount /dev/vdb /mnt/es-tree; \
	printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; \
	/tmp/es-tree-count-workload /mnt/es-tree/work; sync; \
	echo 3 > /proc/sys/vm/drop_caches; sync; \
	cat /sys/kernel/debug/ext4/vdb/invariant_summary > /tmp/es-tree-summary; \
	cat /tmp/es-tree-summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; \
	dmesg | tail -200 > /tmp/es-tree-dmesg; cat /tmp/es-tree-dmesg; \
	! grep -Eq 'KASAN:|BUG:|Oops:|EXT4 invariant violation' /tmp/es-tree-dmesg; \
	! grep -q '^model_extent_status_tree_violations=[1-9]' /tmp/es-tree-summary; \
	grep -Eq '^model_extent_status_tree_checks=[1-9][0-9]*$' /tmp/es-tree-summary; \
	umount /mnt/es-tree; systemctl poweroff" > "${run_dir}/runtime.txt" 2>&1

for _ in $(seq 1 60); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo "guest did not stop cleanly: ${run_dir}" >&2
	exit 1
fi
e2fsck -fn "${run_dir}/data.raw" > "${run_dir}/e2fsck.txt" 2>&1
printf 'es_tree_count=passed run_dir=%s\n' "${run_dir}"
tail -40 "${run_dir}/runtime.txt"
