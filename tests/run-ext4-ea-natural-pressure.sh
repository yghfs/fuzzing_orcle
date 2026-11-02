#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$root_dir/work"
kernel="${EXT4_EA_PRESSURE_KERNEL:-$work_dir/kernels/ext4-count-normal-new/bzImage}"
base="$work_dir/debian-clean-smalljournal.img"
port="${EXT4_EA_PRESSURE_PORT:-12281}"
run="${EXT4_EA_PRESSURE_RUN_DIR:-$work_dir/ext4-ea-natural-pressure-$(date +%Y%m%d%H%M%S)-$$}"
mkdir -p "$run"
gcc -O2 -Wall -Wextra -Werror -static -o "$run/workload" "$root_dir/tests/ext4-ea-natural-pressure.c"
qemu-img create -q -f raw "$run/test.raw" 512M
mke2fs -q -F -t ext4 -O 'ea_inode,^orphan_file,^64bit' -b 4096 -N 32768 -m 0 -J size=4 -E lazy_itable_init=0 "$run/test.raw"
e2fsck -fn "$run/test.raw" >"$run/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "$base" "$run/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "$run/test.raw" "$run/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 512M -smp 2 -kernel "$kernel" \
  -drive "file=$run/root.qcow2,format=qcow2,if=virtio" -drive "file=$run/test.qcow2,format=qcow2,if=virtio" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=n0 \
  -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
  -display none -monitor none -serial "file:$run/serial.log" -daemonize -pidfile "$run/qemu.pid"
pid=$(<"$run/qemu.pid"); trap 'kill "$pid" 2>/dev/null || true' EXIT
ssh_opts=(-i "$work_dir/debian.id_rsa" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do ssh "${ssh_opts[@]}" root@127.0.0.1 'test -r /sys/kernel/debug/ext4' >/dev/null 2>&1 && break; sleep 1; done
scp -q -i "$work_dir/debian.id_rsa" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$run/workload" root@127.0.0.1:/tmp/ea-pressure
ssh "${ssh_opts[@]}" root@127.0.0.1 'mkdir -p /mnt/test; mount /dev/vdb /mnt/test; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 "timeout --foreground 300s /tmp/ea-pressure /mnt/test ${EXT4_EA_PRESSURE_FILES:-200} ${EXT4_EA_PRESSURE_MIB:-384}; rc=\$?; echo workload_rc=\$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'ea_references|xattr_container_release|EXT4 invariant violation|Out of memory|ENOMEM|BUG:' | tail -200; sync; umount /mnt/test; poweroff -f" >"$run/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
qemu-img convert -q -f qcow2 -O raw "$run/test.qcow2" "$run/recovered.raw"
set +e; e2fsck -fn "$run/recovered.raw" >"$run/fsck.txt" 2>&1; fsck_rc=$?; set -e
printf 'run=%s runtime_rc=%s fsck_rc=%s\n' "$run" "$runtime_rc" "$fsck_rc" | tee "$run/report.txt"
rg -n 'ea_pressure|error_exit|model_ea|violations_total|snapshot OK|xattr_container|Free .*count|inode bitmap' "$run/runtime.txt" "$run/fsck.txt" || true
