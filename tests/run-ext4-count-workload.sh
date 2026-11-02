#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${work_dir}/ext4-count-workload-$(date +%Y%m%d%H%M%S)-$$"
kernel="${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage"
port="${EXT4_COUNT_PORT:-11740}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/ext4-count-workload" \
  "${root_dir}/tests/ext4-count-workload.c"
qemu-img create -q -f qcow2 -F raw \
  -b "${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}" "${run_dir}/root.qcow2"
for kind in regular bigalloc; do
  qemu-img create -q -f raw "${run_dir}/${kind}.raw" 128M
  options=(-O ^orphan_file -b 4096 -N 4096 -J size=4 -E lazy_itable_init=0)
  [[ "${kind}" != bigalloc ]] || options+=(-O bigalloc,^orphan_file -C 65536)
  mke2fs -q -F -t ext4 "${options[@]}" "${run_dir}/${kind}.raw"
done
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
  -kernel "${kernel}" -drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
  -drive "file=${run_dir}/regular.raw,format=raw,if=virtio" \
  -drive "file=${run_dir}/bigalloc.raw,format=raw,if=virtio" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
  -device virtio-net-pci,netdev=n0 \
  -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0' \
  -display none -monitor none -serial "file:${run_dir}/serial.log" \
  -daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT
ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes \
  -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR)
for _ in $(seq 1 120); do
  if ssh "${ssh_opts[@]}" root@127.0.0.1 \
    'test -r /sys/kernel/debug/ext4/vda/invariant_summary' >/dev/null 2>&1; then break; fi
  sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 'test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null "${run_dir}/ext4-count-workload" root@127.0.0.1:/tmp/
for pair in regular:vdb bigalloc:vdc; do
  kind="${pair%:*}"; device="${pair#*:}"
  ssh "${ssh_opts[@]}" root@127.0.0.1 \
    "set -eu; mkdir -p /mnt/count; mount /dev/${device} /mnt/count; \
     printf 1 > /sys/kernel/debug/ext4/${device}/invariant_reset; \
     /tmp/ext4-count-workload /mnt/count/run; \
     sync; sleep 1; \
     for attempt in \$(seq 1 10); do \
       cat /sys/kernel/debug/ext4/${device}/invariant_summary > /tmp/count-summary; \
       if grep -qE '^model_block_bitmap_checks=[1-9][0-9]*$' /tmp/count-summary; then break; fi; \
       sleep 0.2; \
     done; cat /tmp/count-summary; umount /mnt/count" \
    > "${run_dir}/${kind}.txt" 2>&1
  bash "${root_dir}/tests/assert-ext4-count-model.sh" "${run_dir}/${kind}.txt" reservation control
  bash "${root_dir}/tests/assert-ext4-count-model.sh" "${run_dir}/${kind}.txt" directory_link_count control yes
  bash "${root_dir}/tests/assert-ext4-count-model.sh" "${run_dir}/${kind}.txt" inode_bitmap control
  bash "${root_dir}/tests/assert-ext4-count-model.sh" "${run_dir}/${kind}.txt" block_bitmap control
done
ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg > "${run_dir}/dmesg.txt"
if rg -q 'KASAN:|BUG:|Oops:|possible circular locking|EXT4 invariant violation' \
  "${run_dir}/dmesg.txt"; then
  echo "unexpected kernel diagnostic: ${run_dir}/dmesg.txt" >&2
  exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do
	 kill -0 "${qemu_pid}" 2>/dev/null || break
  sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
  echo "guest did not stop cleanly" >&2
  exit 1
fi
for kind in regular bigalloc; do
  e2fsck -fn "${run_dir}/${kind}.raw" > "${run_dir}/${kind}-fsck.txt" 2>&1
done
printf 'normal count workloads: passed (fault injection disabled)\nrun_dir=%s\n' "${run_dir}"
