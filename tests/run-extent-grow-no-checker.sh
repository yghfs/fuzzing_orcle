#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build="${root_dir}/experiments/ext4-invariant-checker/build/linux-invariant-checker"
kernel="${build}/arch/x86/boot/bzImage"
root_base="${root_dir}/work/debian-clean-smalljournal.img"
run_dir="${1:-${root_dir}/work/mainline-style-extent-grow-$(date +%Y%m%d-%H%M%S)}"

mkdir -p "${run_dir}"

test_image="${run_dir}/test.raw"
qemu-img create -q -f raw "${test_image}" 512M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 16384 -J size=4 "${test_image}"

root_prep="${run_dir}/root-base.raw"
cp --sparse=always "${root_base}" "${root_prep}"
tune2fs -O ^orphan_file "${root_prep}" >/dev/null 2>&1
qemu_root="${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_prep}" "${qemu_root}"

gcc -O2 -Wall -Wextra -static -pthread -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-extent-grow-persistent-iblocks.c"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024M -smp 4 \
	-kernel "${kernel}" \
	-drive "file=${qemu_root},format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:11947-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=0' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"

qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 120); do
	ssh -qi "${root_dir}/work/debian.id_rsa" -p 11947 -o BatchMode=yes \
		-o StrictHostKeyChecking=no root@127.0.0.1 \
		'mount /dev/vdb /mnt/ext4-test 2>/dev/null; true' >/dev/null 2>&1 && break
	sleep 1
done

scp -qi "${root_dir}/work/debian.id_rsa" -P 11947 -o StrictHostKeyChecking=no \
	"${run_dir}/workload" root@127.0.0.1:/tmp/extent-grow 2>/dev/null

ssh -qi "${root_dir}/work/debian.id_rsa" -p 11947 -o BatchMode=yes \
	-o StrictHostKeyChecking=no root@127.0.0.1 \
	'echo 0 > /sys/kernel/debug/ext4/vdb/invariant_reset 2>/dev/null; true; /tmp/extent-grow /mnt/ext4-test 256 20; sync; umount /mnt/ext4-test; poweroff' \
	>"${run_dir}/runtime.txt" 2>&1 || true

sleep 5; for _ in $(seq 1 30); do kill -0 "${qemu_pid}" 2>/dev/null || break; sleep 1; done

e2fsck -fn "${test_image}" >"${run_dir}/fsck.txt" 2>&1 || true

echo "=== WITHOUT CHECKER - fsck 结果 ==="
grep -E "i_blocks|块位图" "${run_dir}/fsck.txt" | head -20 || echo "No i_blocks errors"

echo ""
echo "debugfs icheck (查找 inode 引用):"
debugfs -R 'icheck 32833 33285 33290' "${test_image}" 2>/dev/null | head -5

echo ""
echo "debugfs testb (块位图状态):"
for blk in 32833 33285; do
	debugfs -R "testb $blk" "${test_image}" 2>/dev/null
done

echo ""
ls -lh "${test_image}"
