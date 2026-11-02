#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ext4-fault-env.sh"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$root/work"
run="${EXT4_IOERR_RUN_DIR:-$work/ext4-natural-io-error-$(date +%Y%m%d%H%M%S)-$$}"
base="${EXT4_IOERR_BASE_IMAGE:-/tmp/ext4-inode-free-base.raw}"
root_base="${EXT4_BASE_IMAGE:-$work/debian-clean-smalljournal.img}"
port="${EXT4_IOERR_PORT:-11563}"
ioerr_workers="${EXT4_IOERR_WORKERS:-16}"
ioerr_loops="${EXT4_IOERR_LOOPS:-2000}"
ioerr_sync_every="${EXT4_IOERR_SYNC_EVERY:-16}"
ioerr_operation="${EXT4_IOERR_OPERATION:-mixed}"
event_cap="${EXT4_IOERR_EVENT_CAP:-65536}"
mkdir -p "$run"
if [[ ! -e "$base" ]]; then
	qemu-img create -q -f raw "$base" 1G
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 32768 -E lazy_itable_init=0 "$base" 262144
fi
e2fsck -fn "$base" > "$run/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "$root_base" "$run/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "$base" "$run/test.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 2048 -smp 8 \
	-kernel "$EXT4_FAULT_KERNEL" \
	-drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=$run/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=${event_cap} ext4.ext4_fault_mode=0" \
	-display none -monitor none -serial "file:$run/serial.log" -daemonize -pidfile "$run/qemu.pid"
qpid="$(<"$run/qemu.pid")"
cleanup() { kill "$qpid" 2>/dev/null || true; }
trap cleanup EXIT
ssh_opts=(-i "$work/debian.id_rsa" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 'test -e /dev/mapper/control && test -r /sys/kernel/debug/ext4' >/dev/null 2>&1; then break; fi
	sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 'mkdir -p /mnt/ioerr; sectors=$(blockdev --getsz /dev/vdb); dmsetup create ext4_ioerr --table "0 $sectors linear /dev/vdb 0"; mount -o errors=continue /dev/mapper/ext4_ioerr /mnt/ioerr; mount -o remount,commit=1 /mnt/ioerr; printf 1 > /sys/kernel/debug/ext4/dm-0/invariant_reset'
ssh "${ssh_opts[@]}" root@127.0.0.1 'dd if=/dev/zero of=/mnt/ioerr/victim bs=4096 count=4 conv=fsync status=none; sync -f /mnt/ioerr'
set +e
if [[ "$ioerr_operation" == create ]]; then
	worker_op=': > "$path" || break'
	worker_setup=':'
elif [[ "$ioerr_operation" == unlink ]]; then
	worker_setup=': > /mnt/ioerr/victim-$w || exit; sync -f /mnt/ioerr'
	worker_op='sleep 1; rm -f /mnt/ioerr/victim-$w || break'
else
	worker_op=': > "$path" || break; rm -f "$path" || true'
	worker_setup=':'
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 "set -x; sectors=\$(blockdev --getsz /dev/vdb); rest=\$((sectors - 16)); echo failing_home_block=1 error_sectors=8-15 operation=${ioerr_operation}; pids=(); for w in \$(seq 1 ${ioerr_workers}); do ${worker_setup}; (for i in \$(seq 1 ${ioerr_loops}); do path=/mnt/ioerr/w-\$w-\$i; ${worker_op}; if (( i % ${ioerr_sync_every} == 0 )); then sync -f /mnt/ioerr >/dev/null 2>&1 || break; fi; done) > /tmp/ioerr-worker-\$w.log 2>&1 & pids+=(\"\$!\"); done; sleep 0.25; dmsetup suspend ext4_ioerr; table=\$(printf \"0 8 linear /dev/vdb 0\\n8 8 error\\n16 %s linear /dev/vdb 16\" \"\$rest\"); dmsetup reload ext4_ioerr --table \"\$table\"; dmsetup resume ext4_ioerr; for p in \"\${pids[@]}\"; do wait \"\$p\" || true; done; for f in /tmp/ioerr-worker-*.log; do echo worker_log=\$f; cat \"\$f\"; done; stat -f /mnt/ioerr; ls -la /mnt/ioerr; : > /mnt/ioerr/post-error-create; echo post_error_create_rc=\$?; cat /sys/kernel/debug/ext4/dm-0/invariant_summary; dmesg | tail -100; dmsetup suspend ext4_ioerr; dmsetup reload ext4_ioerr --table \"0 \$sectors linear /dev/vdb 0\"; dmsetup resume ext4_ioerr; sync; umount /mnt/ioerr; systemctl poweroff" > "$run/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 45); do kill -0 "$qpid" 2>/dev/null || break; sleep 1; done
if kill -0 "$qpid" 2>/dev/null; then echo 'guest did not stop cleanly' > "$run/fsck.txt"; fsck_rc=125; else
	qemu-img convert -q -f qcow2 -O raw "$run/test.qcow2" "$run/recovered.raw"
	set +e
	e2fsck -E journal_only -y "$run/recovered.raw" > "$run/journal.txt" 2>&1; journal_rc=$?
	e2fsck -fn "$run/recovered.raw" > "$run/fsck.txt" 2>&1; fsck_rc=$?
	set -e
fi
if rg -qi 'count wrong|corrupt|inconsisten|fix\? yes|filesystem still has errors' "$run/fsck.txt"; then
	fsck_findings=1
else
	fsck_findings=0
fi
printf 'run_dir=%s runtime_rc=%s journal_rc=%s fsck_rc=%s fsck_findings=%s\n' \
	"$run" "$runtime_rc" "${journal_rc:-unknown}" "$fsck_rc" \
	"$fsck_findings" | tee "$run/report.txt"
rg 'target_error_|io_stage_|journal_|model_.*(checks|violations|unverified)=|error_exit_.*(hits|verified|eio|last_errno|last_expected|last_actual)=|violations_total=|post_error_create_rc=|EXT4-fs.*(error|warning)' \
	"$run/runtime.txt" >> "$run/report.txt" || true
rg -i 'count wrong|corrupt|inconsisten|fix\? yes|filesystem still has errors' \
	"$run/fsck.txt" >> "$run/report.txt" || true
cat "$run/report.txt"
