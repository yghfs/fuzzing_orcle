#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ext4-fault-env.sh"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
run_dir="${EXT4_MBALLOC_FREE_RUN_DIR:-/tmp/ext4-mballoc-free-$(date +%Y%m%d%H%M%S)-$$}"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_MBALLOC_FREE_BASE_IMAGE:-/tmp/ext4-mballoc-free-base.img}"
port="${EXT4_MBALLOC_FREE_PORT:-11591}"
mkdir -p "$run_dir"
gcc -O2 -Wall -Wextra -Werror -static -o "$run_dir/ext4-mballoc-free-failure" "$root_dir/tests/ext4-mballoc-free-failure.c"
if [[ ! -e "$test_base" ]]; then qemu-img create -q -f raw "$test_base" 128M; mke2fs -q -F -t ext4 -O bigalloc,^orphan_file -C 65536 -N 4096 -J size=4 "$test_base"; fi
qemu-img create -q -f qcow2 -F raw -b "$base_image" "$run_dir/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "$test_base" "$run_dir/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024 -smp 2 -kernel "${EXT4_FAULT_KERNEL}" -drive "file=$run_dir/root.qcow2,format=qcow2,if=virtio" -drive "file=$run_dir/test.qcow2,format=qcow2,if=virtio" -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=n0 -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096 ext4.ext4_fault_mode=0' -display none -monitor none -serial "file:$run_dir/serial.log" -daemonize -pidfile "$run_dir/qemu.pid"
cleanup(){ [[ -s "$run_dir/qemu.pid" ]] && kill "$(cat "$run_dir/qemu.pid")" 2>/dev/null || true; }; trap cleanup EXIT
ssh_opts=(-i "$work_dir/debian.id_rsa" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do if ssh "${ssh_opts[@]}" root@127.0.0.1 'mkdir -p /mnt/ext4-free; mountpoint -q /mnt/ext4-free || mount /dev/vdb /mnt/ext4-free; test -r /sys/module/ext4/parameters/ext4_fault_mode' >/dev/null 2>&1; then break; fi; sleep 1; done
scp -q -i "$work_dir/debian.id_rsa" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$run_dir/ext4-mballoc-free-failure" root@127.0.0.1:/tmp/
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 '/tmp/ext4-mballoc-free-failure /mnt/ext4-free/free; sync; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | tail -100; sync; umount /mnt/ext4-free; systemctl poweroff' > "$run_dir/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 45); do kill -0 "$(cat "$run_dir/qemu.pid")" 2>/dev/null || break; sleep 1; done
qemu-img convert -q -f qcow2 -O raw "$run_dir/test.qcow2" "$run_dir/recovered.raw"
set +e
e2fsck -E journal_only -y "$run_dir/recovered.raw" > "$run_dir/journal-replay.txt" 2>&1; journal_rc=$?
e2fsck -fn "$run_dir/recovered.raw" > "$run_dir/e2fsck.txt" 2>&1; fsck_rc=$?
set -e
printf 'run_dir=%s runtime_rc=%s journal_rc=%s fsck_rc=%s\n' "$run_dir" "$runtime_rc" "$journal_rc" "$fsck_rc" | tee "$run_dir/report.txt"
tail -40 "$run_dir/runtime.txt" | tee -a "$run_dir/report.txt"
tail -30 "$run_dir/e2fsck.txt" | tee -a "$run_dir/report.txt"
