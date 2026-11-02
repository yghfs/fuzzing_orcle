#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base_image="${EXT4_TEST_BASE_IMAGE:-${work_dir}/ext4-runtime-clean.img}"
port="${EXT4_ENOSPC_PORT:-11370}"
checker_device="${EXT4_CHECKER_DEVICE:-vdb}"
run_dir="${work_dir}/ext4-enospc-rollback-$(date +%Y%m%d%H%M%S)-$$"
overlay="${run_dir}/disk.qcow2"
test_overlay="${run_dir}/test-filesystem.qcow2"
pid_file="${run_dir}/qemu.pid"
serial_log="${run_dir}/serial.log"
ssh_key="${work_dir}/debian.id_rsa"
program="${run_dir}/ext4-enospc-rollback"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -static -o "${program}" \
  "${root_dir}/tests/ext4-enospc-rollback.c"
if [[ ! -e "${test_base_image}" ]]; then
  qemu-img create -f raw "${test_base_image}" 2G >/dev/null
  mke2fs -q -F -t ext4 "${test_base_image}"
fi
e2fsck -fn "${test_base_image}" > "${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -f qcow2 -F raw -b "${base_image}" "${overlay}" >/dev/null
qemu-img create -f qcow2 -F raw -b "${test_base_image}" "${test_overlay}" >/dev/null
cleanup() {
  if [[ -s "${pid_file}" ]]; then
    kill "$(<"${pid_file}")" 2>/dev/null || true
  fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024 -smp 2 \
  -kernel "${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage" \
  -drive "file=${overlay},format=qcow2,if=virtio" \
  -drive "file=${test_overlay},format=qcow2,if=virtio" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
  -device virtio-net-pci,netdev=n0 \
  -append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536" \
  -display none -monitor none -serial "file:${serial_log}" -daemonize \
  -pidfile "${pid_file}"

for _ in $(seq 1 120); do
  if ssh "${ssh_opts[@]}" root@127.0.0.1 \
      "mountpoint -q /mnt/ext4-runtime || { mkdir -p /mnt/ext4-runtime && mount /dev/${checker_device} /mnt/ext4-runtime; }; test -r /sys/kernel/debug/ext4/${checker_device}/invariant_summary" \
      >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
  "mountpoint -q /mnt/ext4-runtime && test -r /sys/kernel/debug/ext4/${checker_device}/invariant_summary"
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/
ssh "${ssh_opts[@]}" root@127.0.0.1 \
  "printf 1 > /sys/kernel/debug/ext4/${checker_device}/invariant_reset"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
  'chmod +x /tmp/ext4-enospc-rollback'
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
  'timeout --foreground 180s /tmp/ext4-enospc-rollback /mnt/ext4-runtime/ext4-enospc' \
  > "${run_dir}/program.txt" 2>&1
program_rc=$?
set -e
printf 'program_rc=%s\n' "${program_rc}" >> "${run_dir}/program.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 \
  "cat /sys/kernel/debug/ext4/${checker_device}/invariant_summary" \
  > "${run_dir}/checker-after.txt" 2>&1 || true
for n in 1 2 3; do
  ssh "${ssh_opts[@]}" root@127.0.0.1 \
    "cat /sys/kernel/debug/ext4/${checker_device}/invariant_summary" \
    > "${run_dir}/quiet-${n}.txt" 2>&1 || true
  sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
  "cat /sys/kernel/debug/ext4/${checker_device}/invariant_check" \
  > "${run_dir}/event-ring.txt" 2>&1 || true
ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg > "${run_dir}/dmesg.txt" 2>&1 || true
ssh "${ssh_opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do
  if [[ -s "${pid_file}" ]] && ! kill -0 "$(<"${pid_file}")" 2>/dev/null; then
    break
  fi
  sleep 1
done
if [[ -s "${pid_file}" ]] && kill -0 "$(<"${pid_file}")" 2>/dev/null; then
  echo 'guest did not stop cleanly' > "${run_dir}/e2fsck.txt"
  fsck_rc=125
else
  qemu-img convert -f qcow2 -O raw "${test_overlay}" "${run_dir}/test-recovered.raw"
  set +e
  e2fsck -E journal_only -y "${run_dir}/test-recovered.raw" > "${run_dir}/journal-replay.txt" 2>&1
  journal_rc=$?
  e2fsck -fn "${run_dir}/test-recovered.raw" > "${run_dir}/e2fsck.txt" 2>&1
  fsck_rc=$?
  set -e
  rg 'Inode bitmap differences|Block bitmap differences|Free .* count|Multiply-claimed|UNEXPECTED|Filesystem still has errors|WARNING: Filesystem' \
    "${run_dir}/baseline-fsck.txt" > "${run_dir}/baseline-findings.txt" || true
  rg 'Inode bitmap differences|Block bitmap differences|Free .* count|Multiply-claimed|UNEXPECTED|Filesystem still has errors|WARNING: Filesystem' \
    "${run_dir}/e2fsck.txt" > "${run_dir}/candidate-findings.txt" || true
  if diff -u "${run_dir}/baseline-findings.txt" "${run_dir}/candidate-findings.txt" \
      > "${run_dir}/fsck-findings.diff"; then
    findings_match=1
  else
    findings_match=0
  fi
  printf 'journal_rc=%s fsck_rc=%s\n' "${journal_rc}" "${fsck_rc}" >> "${run_dir}/e2fsck.txt"
fi
{
  printf 'program_rc=%s\n' "${program_rc}"
  sed -n '1,80p' "${run_dir}/program.txt"
  printf '%s\n' '--- checker-after ---'
  rg -n '^(snapshot|violations_total|unverified_total|snapshot_races|tx_underflows|orphan_|events=|group_capacity)' "${run_dir}/checker-after.txt" || true
  printf '%s\n' '--- quiet-3 ---'
  rg -n '^(snapshot|violations_total|unverified_total|snapshot_races|snapshots=|tx_underflows|orphan_|events=|group_capacity)' "${run_dir}/quiet-3.txt" || true
  printf 'fsck_findings_match_baseline=%s\n' "${findings_match:-unknown}"
  printf '%s\n' '--- baseline fsck findings ---'
  cat "${run_dir}/baseline-findings.txt" 2>/dev/null || true
  printf '%s\n' '--- candidate fsck findings ---'
  cat "${run_dir}/candidate-findings.txt" 2>/dev/null || true
  printf '%s\n' '--- dmesg ---'
  rg -n 'KASAN:|BUG:|Oops|EXT4-fs.*error|general protection|lockup|hung task' "${run_dir}/dmesg.txt" || true
  printf '%s\n' '--- fsck ---'
  tail -45 "${run_dir}/e2fsck.txt"
} | tee "${run_dir}/report.txt"
printf 'run_dir=%s\n' "${run_dir}"
