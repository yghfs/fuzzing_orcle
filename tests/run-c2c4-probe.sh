#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${work_dir}/c2c4-run-$(date +%Y%m%d%H%M%S)-$$"
kernel="${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage"
port="${EXT4_C2C4_PORT:-11821}"
memory="${EXT4_C2C4_MEMORY:-1024}"
test_base="${EXT4_C2C4_BASE:-${work_dir}/ext4-dir-nlink-1g.raw}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
  -o ConnectTimeout=4 -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
mkdir -p "${run_dir}"
gcc -O2 -Wall -static -o "${run_dir}/c2" "${root_dir}/tests/c2-mkdir-enospc-retry.c"
gcc -O2 -Wall -static -o "${run_dir}/c4" "${root_dir}/tests/c4-rename-error-partial.c"
e2fsck -fn "${test_base}" >"${run_dir}/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "${work_dir}/debian-clean-smalljournal.img" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${memory}M" -smp 2 \
  -kernel "${kernel}" \
  -drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
  -drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
  -device virtio-net-pci,netdev=n0 \
  -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
  -display none -monitor none -serial "file:${run_dir}/serial.log" \
  -daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 120); do
  if ssh "${ssh_opts[@]}" root@127.0.0.1 \
    'test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1; then break; fi
  sleep 1
done
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null "${run_dir}/c2" "${run_dir}/c4" root@127.0.0.1:/tmp/
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 "
  set -x
  mkdir -p /mnt/c2c4 && mount /dev/vdb /mnt/c2c4
  printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset
  /tmp/c2 /mnt/c2c4/work2 2>&1 | tail -5
  /tmp/c4 /mnt/c2c4/work4 2>&1
  sync; sleep 2
  for i in 1 2 3; do
    cat /sys/kernel/debug/ext4/vdb/invariant_summary > /tmp/sum-$i
    sleep 1
  done
  cat /tmp/sum-1 /tmp/sum-2 /tmp/sum-3
  systemctl poweroff
" >"${run_dir}/runtime.txt" 2>&1
set -e
for _ in $(seq 1 90); do
  if ! kill -0 "${qemu_pid}" 2>/dev/null; then break; fi
  sleep 1
done
qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" "${run_dir}/recovered.raw"
e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
fsck_rc=$?
printf 'run_dir=%s fsck_rc=%s\n' "${run_dir}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'violations_total|model_.*violations|last_violation|rename_rc|mkdir_fail|Extended attribute|Group descriptor|Inode|ref count' \
  "${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tail -40 | tee -a "${run_dir}/report.txt" || true

