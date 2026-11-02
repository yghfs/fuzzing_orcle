#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ext4-fault-env.sh"
r="$(cd "$(dirname "$0")/.."; pwd)"; w="$r/work"
case_name="${EXT4_NS_CASE:?set EXT4_NS_CASE=mkdir}"
d="${EXT4_NS_RUN_DIR:-$w/ext4-ns-$case_name-$(date +%Y%m%d%H%M%S)-$$}"
b="${EXT4_BASE_IMAGE:-$w/debian-clean-smalljournal.img}"; tb="${EXT4_NS_BASE_IMAGE:-/tmp/ext4-ns-base.img}"
port="${EXT4_NS_PORT:-11890}"; mkdir -p "$d"
fault="${EXT4_NS_FAULT:-34}"
summary_reset="${EXT4_NS_RESET_SUMMARY:-0}"
case "$case_name" in
 mkdir) src="$r/tests/ext4-mkdir-parent-dirty-failure.c"; prog=ext4-mkdir-parent-dirty-failure; args="/mnt/ext4-ns $fault";;
 *) echo bad case >&2; exit 2;;
esac
gcc -O2 -Wall -Wextra -Werror -static -o "$d/$prog" "$src"
if [[ ! -e "$tb" ]]; then qemu-img create -q -f raw "$tb" 256M; mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 8192 -J size=4 "$tb"; fi
e2fsck -fn "$tb" > "$d/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "$b" "$d/root.qcow2"; qemu-img create -q -f qcow2 -F raw -b "$tb" "$d/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1024 -smp 2 -kernel "${EXT4_FAULT_KERNEL}" -drive "file=$d/root.qcow2,format=qcow2,if=virtio" -drive "file=$d/test.qcow2,format=qcow2,if=virtio" -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=n0 -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096 ext4.ext4_fault_mode=0' -display none -monitor none -serial "file:$d/serial.log" -daemonize -pidfile "$d/qemu.pid"
trap 'kill "$(cat "$d/qemu.pid")" 2>/dev/null || true' EXIT
o=(-i "$w/debian.id_rsa" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for i in $(seq 1 120); do ssh "${o[@]}" root@127.0.0.1 'mkdir -p /mnt/ext4-ns; mountpoint -q /mnt/ext4-ns || mount /dev/vdb /mnt/ext4-ns; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1 && break || sleep 1; done
scp -q -i "$w/debian.id_rsa" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$d/$prog" root@127.0.0.1:/tmp/
reset_cmd="true"; if [[ "$summary_reset" == 1 ]]; then reset_cmd="printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset"; fi
set +e; ssh "${o[@]}" root@127.0.0.1 "/tmp/$prog $args; sync; sleep 1; $reset_cmd; sleep 1; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | tail -100; umount /mnt/ext4-ns; systemctl poweroff" > "$d/runtime.txt" 2>&1; rr=$?; set -e
for i in $(seq 1 45); do kill -0 "$(cat "$d/qemu.pid")" 2>/dev/null || break; sleep 1; done
qemu-img convert -q -f qcow2 -O raw "$d/test.qcow2" "$d/recovered.raw"
set +e; e2fsck -E journal_only -y "$d/recovered.raw" > "$d/journal.txt" 2>&1; jr=$?; e2fsck -fn "$d/recovered.raw" > "$d/fsck.txt" 2>&1; fr=$?; set -e
rg 'Inode bitmap differences|Block bitmap differences|Free .* count|Inode .* has .* links|inode .* is a zero-length directory|Entry .* has inode|Filesystem still has errors' "$d/fsck.txt" > "$d/candidate-findings.txt" || true
printf 'case=%s run_dir=%s runtime_rc=%s journal_rc=%s fsck_rc=%s\n' "$case_name" "$d" "$rr" "$jr" "$fr" | tee "$d/report.txt"
rg -n 'mkdir_rc|create_rc|link_rc|fault:|violations|snapshot|events_dropped' "$d/runtime.txt" | tee -a "$d/report.txt" || true
cat "$d/candidate-findings.txt" | tee -a "$d/report.txt"; tail -25 "$d/fsck.txt" | tee -a "$d/report.txt"
