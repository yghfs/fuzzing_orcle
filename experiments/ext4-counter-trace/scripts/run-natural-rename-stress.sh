#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
run=${1:-$(mktemp -d "$project/results/natural-rename-XXXXXXXX")}
port=${2:-11920}
seconds=${NATURAL_RENAME_SECONDS:-20}
workers=${NATURAL_RENAME_WORKERS:-4}
fillers=${NATURAL_RENAME_FILLERS:-640}
mount_options=${NATURAL_RENAME_MOUNT_OPTIONS:-}
block_size=${NATURAL_RENAME_BLOCK_SIZE:-4096}
block_count=${NATURAL_RENAME_BLOCK_COUNT:-40000}
format_features=${NATURAL_RENAME_FORMAT_FEATURES:-orphan_file}
inode_count=${NATURAL_RENAME_INODES:-16384}

mkdir -p "$run"
if find "$run" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
	echo "run directory must be empty: $run" >&2
	exit 1
fi
run=$(cd "$run" && pwd)
key="$root/work/debian.id_rsa"
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
base_image="$root/work/debian-clean-smalljournal.img"
disk_image="$run/test.raw"
recovery_image="$run/recovery.raw"
guest_bin="$run/natural-rename-stress"
qemu_pidfile="$run/qemu.pid"

test -s "$kernel"
test -s "$base_image"
test -s "$key"
command -v gcc >/dev/null
command -v qemu-img >/dev/null
command -v qemu-system-x86_64 >/dev/null
command -v ssh >/dev/null
command -v scp >/dev/null
command -v mke2fs >/dev/null
command -v e2fsck >/dev/null
command -v sha256sum >/dev/null

gcc -O2 -Wall -Wextra -Werror -static -o "$guest_bin" \
	"$project/tests/natural-rename-stress.c"
qemu-img create -q -f qcow2 -F raw -b "$base_image" "$run/root.qcow2"
qemu-img create -q -f raw "$disk_image" 200M
mke2fs -q -F -t ext4 -O "$format_features" -b "$block_size" -N "$inode_count" \
	-E lazy_itable_init=0 "$disk_image" "$block_count"
e2fsck -fn "$disk_image" > "$run/baseline-fsck.txt" 2>&1 || {
	rc=$?
	[ "$rc" -le 1 ] || exit "$rc"
}

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "$kernel" -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=$disk_image,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
	-display none -monitor none -serial "file:$run/serial.log" \
	-daemonize -pidfile "$qemu_pidfile"
qemu_pid=$(<"$qemu_pidfile")
cleanup() {
	if [[ -s "$qemu_pidfile" ]]; then
		kill "$qemu_pid" 2>/dev/null || true
	fi
}
trap cleanup EXIT

ssh_opts=(-i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
scp_opts=(-i "$key" -P "$port" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

ready=0
for _ in $(seq 1 180); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 'test -r /etc/os-release' \
		>/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 1
done
[[ "$ready" == 1 ]] || { tail -80 "$run/serial.log"; exit 1; }

scp -q "${scp_opts[@]}" "$guest_bin" \
		"$project/tests/guest-natural-rename-stress.sh" root@127.0.0.1:/tmp/
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"NATURAL_RENAME_SECONDS='$seconds' NATURAL_RENAME_WORKERS='$workers' NATURAL_RENAME_FILLERS='$fillers' \
	 EXT4_FAULT_TRACE_BUFFER_KB=32768 \
	 sh /tmp/guest-natural-rename-stress.sh \
	 --mount-options '$mount_options' --fixture /tmp/natural-rename-stress \
	 --evidence-dir /tmp/fault-formula" > "$run/guest.txt" 2>&1
guest_rc=$?
set -e
scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/fault-formula "$run/" || \
	echo 'guest evidence fetch failed' > "$run/fetch-error.txt"
ssh "${ssh_opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
	kill -0 "$qemu_pid" 2>/dev/null || break
	sleep 1
done
if kill -0 "$qemu_pid" 2>/dev/null; then
	echo 'guest did not power off cleanly' > "$run/poweroff-error.txt"
	kill "$qemu_pid" 2>/dev/null || true
fi

cp --reflink=auto --preserve=all "$disk_image" "$recovery_image"
set +e
e2fsck -fn "$disk_image" > "$run/fsck-before-recovery.txt" 2>&1
fsck_before_rc=$?
e2fsck -fn "$recovery_image" > "$run/fsck.txt" 2>&1
fsck_rc=$?
python3 "$project/scripts/check-disk.py" "$disk_image" \
	--output "$run/disk-before-recovery.json" > /dev/null 2> "$run/disk-before-recovery-error.txt"
disk_before_rc=$?
python3 "$project/scripts/check-disk.py" "$recovery_image" \
	--output "$run/disk.json" > /dev/null 2> "$run/disk-error.txt"
disk_rc=$?
set -e

grep -E 'jbd2_journal_dirty_metadata|errcode -28|ENOSPC|WARN_ON|Aborting journal' \
	"$run/serial.log" > "$run/jbd2-enospc-grep.txt" || true
cp "$run/guest.txt" "$run/guest-summary.txt"
printf 'guest_rc=%s\nfsck_before_rc=%s\nfsck_rc=%s\ndisk_before_rc=%s\ndisk_rc=%s\n' \
	"$guest_rc" "$fsck_before_rc" "$fsck_rc" "$disk_before_rc" "$disk_rc" \
	> "$run/run-status.txt"

python3 - "$run" <<'PY'
import json
import pathlib
import sys

run = pathlib.Path(sys.argv[1])
raw = run / "fault-formula" / "workload-result.json"
result = json.loads(raw.read_text()) if raw.exists() else {}
grep = (run / "jbd2-enospc-grep.txt").read_text(errors="replace")
summary = {
    "workload": result,
    "jbd2_enospc_log_lines": len([line for line in grep.splitlines() if line.strip()]),
    "run_status": dict(line.split("=", 1) for line in
                        (run / "run-status.txt").read_text().splitlines()
                        if "=" in line),
}
(run / "natural-summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(summary, indent=2, ensure_ascii=False))
PY
printf 'natural_rename_stress_complete\nrun_dir=%s\n' "$run"
