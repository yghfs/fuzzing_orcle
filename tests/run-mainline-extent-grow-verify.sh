#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mainline_build="${root_dir}/experiments/ext4-invariant-checker/build/linux-mainline-verify"
mainline_src="${root_dir}/kernel/linux-mainline"
config="${root_dir}/experiments/ext4-invariant-checker/config/x86_64.config"
root_base="${root_dir}/work/debian-clean-smalljournal.img"
port=11946
run_dir="${1:-${root_dir}/work/mainline-extent-grow-verify-$(date +%Y%m%d-%H%M%S)}"

mkdir -p "${run_dir}" "${mainline_build}"

echo "=== Building mainline kernel (NO CHECKER) ===" 
cp "${config}" "${mainline_build}/.config"
./scripts/config --file "${mainline_build}/.config" \
	--disable EXT4_INVARIANT_CHECKER --disable EXT4_FAULT_INJECTION \
	--disable DEBUG_FS --disable KCOV --disable KASAN 2>/dev/null || true

make -C "${mainline_src}" O="${mainline_build}" ARCH=x86_64 CC=gcc-14 \
	olddefconfig >/dev/null 2>&1
make -C "${mainline_src}" O="${mainline_build}" ARCH=x86_64 CC=gcc-14 \
	-j6 bzImage 2>&1 | tail -5

kernel="${mainline_build}/arch/x86/boot/bzImage"
echo "Kernel: $kernel"

echo "=== Building workload ===" 
gcc -O2 -Wall -Wextra -static -pthread -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-extent-grow-persistent-iblocks.c"

echo "=== Setting up images ===" 
test_image="${run_dir}/test.raw"
qemu-img create -q -f raw "${test_image}" 512M
mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 16384 \
	-J size=4 "${test_image}"

root_prep="${run_dir}/root-base.raw"
cp --sparse=always "${root_base}" "${root_prep}"
tune2fs -O ^orphan_file "${root_prep}" >/dev/null 2>&1
qemu_root="${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${root_prep}" "${qemu_root}"

echo "=== Running mainline test ===" 
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024M -smp 4 \
	-kernel "${kernel}" \
	-drive "file=${qemu_root},format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"

qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 120); do
	ssh -qi "${root_dir}/work/debian.id_rsa" -p ${port} \
		-o BatchMode=yes -o StrictHostKeyChecking=no \
		root@127.0.0.1 'mount /dev/vdb /mnt/ext4-test 2>/dev/null; true' \
		>/dev/null 2>&1 && break
	sleep 1
done

scp -qi "${root_dir}/work/debian.id_rsa" -P ${port} \
	-o StrictHostKeyChecking=no "${run_dir}/workload" \
	root@127.0.0.1:/tmp/extent-grow 2>/dev/null

ssh -qi "${root_dir}/work/debian.id_rsa" -p ${port} \
	-o BatchMode=yes -o StrictHostKeyChecking=no root@127.0.0.1 \
	'/tmp/extent-grow /mnt/ext4-test 256 20 ; sync; umount /mnt/ext4-test; poweroff' \
	>"${run_dir}/runtime.txt" 2>&1 || true

sleep 5
for _ in $(seq 1 30); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done

e2fsck -fn "${test_image}" >"${run_dir}/fsck.txt" 2>&1 || true

echo "=== Results ===" 
grep -E "i_blocks|块位图|Block.*marked" "${run_dir}/fsck.txt" | head -20
echo ""
echo "debugfs icheck:"
debugfs -R 'icheck 32833 33285 33290' "${test_image}" 2>/dev/null | grep -v "^Block"
echo ""
echo "debugfs testb:"
for blk in 32833 33285 33290; do
	debugfs -R "testb $blk" "${test_image}" 2>/dev/null
done

echo ""
echo "Result: $run_dir"
