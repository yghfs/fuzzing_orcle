#!/usr/bin/env bash
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); W=$R/work
K=${EXT4_XATTR_SHUTDOWN_KERNEL:-$W/ext4-new-kbuild/arch/x86/boot/bzImage}; C=${EXT4_XATTR_SHUTDOWN_CONFIG:-$W/ext4-new-kbuild/.config}; P=${EXT4_XATTR_SHUTDOWN_PORT:-11984}; D=${EXT4_XATTR_SHUTDOWN_RUN_DIR:-$W/ext4-xattr-block-shutdown-$(date +%Y%m%d%H%M%S)-$$}; KEY=$W/debian.id_rsa
grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "$C"; grep -q '^# CONFIG_EXT4_FAULT_INJECTION is not set$' "$C"; mkdir -p "$D"
gcc -O2 -Wall -Wextra -Werror -static -o "$D/workload" "$R/tests/ext4-xattr-block-shutdown.c"
qemu-img create -q -f raw "$D/test.raw" 256M; mke2fs -q -F -t ext4 -O '^orphan_file' -b 4096 -N 8192 -J size=4 -E lazy_itable_init=0 "$D/test.raw"
qemu-img create -q -f qcow2 -F raw -b "$W/debian-clean-smalljournal.img" "$D/root.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768M -smp 2 -kernel "$K" -drive file="$D/root.qcow2",format=qcow2,if=virtio -drive file="$D/test.raw",format=raw,if=virtio -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$P-:22 -device virtio-net-pci,netdev=n0 -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' -display none -monitor none -serial file:"$D/serial.log" -daemonize -pidfile "$D/qemu.pid"
Q=$(<"$D/qemu.pid"); trap 'kill "$Q" 2>/dev/null || true' EXIT; O=(-i "$KEY" -p "$P" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do ssh "${O[@]}" root@127.0.0.1 'mkdir -p /mnt/ext4-xattr-shutdown; mountpoint -q /mnt/ext4-xattr-shutdown || mount /dev/vdb /mnt/ext4-xattr-shutdown; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1 && break; sleep 1; done
scp -q -i "$KEY" -P "$P" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$D/workload" root@127.0.0.1:/tmp/ext4-xattr-block-shutdown
ssh "${O[@]}" root@127.0.0.1 'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; /tmp/ext4-xattr-block-shutdown /mnt/ext4-xattr-shutdown; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; sync; umount /mnt/ext4-xattr-shutdown; systemctl poweroff' >"$D/runtime.txt" 2>&1 || true
for _ in $(seq 1 45); do kill -0 "$Q" 2>/dev/null || break; sleep 1; done
set +e; e2fsck -fn "$D/test.raw" >"$D/fsck.txt" 2>&1; FS=$?; set -e
printf 'run_dir=%s fsck_rc=%s\n' "$D" "$FS" | tee "$D/report.txt"
rg -n 'shutdown_|close_|workload_rc|xattr|model_inode_bitmap|violations_total|last_violation|Extended attribute|refcount|Block bitmap|Free .* count' "$D/runtime.txt" "$D/fsck.txt" | tee -a "$D/report.txt" || true
