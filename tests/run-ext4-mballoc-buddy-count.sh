#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${MB_BUDDY_RUN_DIR:-${work_dir}/ext4-mballoc-buddy-$(date +%Y%m%d%H%M%S)-$$}"
kernel="${MB_BUDDY_KERNEL:-${root_dir}/work/ext4-es-kbuild/arch/x86/boot/bzImage}"
base_image="${MB_BUDDY_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
data_size="${MB_BUDDY_DISK_SIZE:-384M}"
port="${MB_BUDDY_PORT:-12167}"

mkdir -p "${run_dir}"
test -r "${kernel}" || { echo "missing kernel: ${kernel}" >&2; exit 2; }
test -r "${base_image}" || { echo "missing base image: ${base_image}" >&2; exit 2; }
gcc -O2 -Wall -Wextra -Werror -static \
	-o "${run_dir}/mballoc-buddy-stress" \
	"${root_dir}/tests/ext4-mballoc-buddy-stress.c"
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
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536" \
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
		'mkdir -p /mnt/mb-buddy; mountpoint -q /mnt/mb-buddy || mount /dev/vdb /mnt/mb-buddy; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then break; fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test -r /sys/kernel/debug/ext4/vdb/invariant_summary && test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/mballoc-buddy-stress" root@127.0.0.1:/tmp/

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'set -eu; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; /tmp/mballoc-buddy-stress /mnt/mb-buddy/work; sync; cat /sys/kernel/debug/ext4/vdb/invariant_summary > /tmp/mb-buddy-summary; cat /tmp/mb-buddy-summary; dmesg | tail -300 > /tmp/mb-buddy-dmesg; cat /tmp/mb-buddy-dmesg; ! grep -Eq "Assertion failure|BUG:|Oops:|EXT4-fs.*error|EXT4 invariant violation" /tmp/mb-buddy-dmesg; ! grep -q "^violations_total=[1-9]" /tmp/mb-buddy-summary; sync; umount /mnt/mb-buddy; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e

for _ in $(seq 1 60); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo "guest did not stop cleanly: ${run_dir}" >&2
	exit 1
fi
e2fsck -fn "${run_dir}/data.raw" > "${run_dir}/e2fsck.txt" 2>&1
grep -q '^mballoc_buddy_stress=complete ' "${run_dir}/runtime.txt"
grep -Eq '^snapshot OK ' "${run_dir}/runtime.txt"
grep -Eq '^violations_total=0$' "${run_dir}/runtime.txt"
! grep -Eq 'Assertion failure|BUG:|Oops:|EXT4-fs.*error|EXT4 invariant violation' \
	"${run_dir}/runtime.txt"
test "${runtime_rc}" -eq 0
printf 'mballoc_buddy_count=passed run_dir=%s\n' "${run_dir}"
tail -30 "${run_dir}/runtime.txt"
tail -12 "${run_dir}/e2fsck.txt"
