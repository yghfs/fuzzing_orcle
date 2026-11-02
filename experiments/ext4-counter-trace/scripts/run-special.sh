#!/usr/bin/env bash
# Targeted verification for the special counter paths: bigalloc rereserve,
# online resize, fast-commit replay and corruption exclusion.
#
# The fast-commit stage needs a real power cut, because any clean shutdown
# flushes the journal and leaves nothing for replay.  The runner therefore
# boots the guest twice: the first boot leaves the fast-commit filesystem
# mounted with a pending fast commit and is killed from the host, and the
# second boot replays it while tracing is already enabled.
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
run=$(mktemp -d "$project/results/special-XXXXXXXX")
port=${EXT4_CT_SPECIAL_PORT:-11872}
fc_mode=${EXT4_CT_FC_MODE:-dir}
key="$root/work/debian.id_rsa"
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
test -s "$kernel"
fc_seed="$run/ext4-fast-commit-inode-replay"
gcc -O2 -Wall -Wextra -Werror -static \
  -o "$fc_seed" "$project/tests/special-fc-workload.c"

qemu-img create -q -f qcow2 -F raw -b "$root/work/debian-clean-smalljournal.img" "$run/root.qcow2"
# One image per targeted path.  The resize image contains a 128 MiB
# filesystem in a 256 MiB device so resize2fs can grow it online.
for kind in bigalloc resize fc corrupt; do
  qemu-img create -q -f raw "$run/$kind.raw" 256M
done
args=(-O ^orphan_file -b 4096 -N 4096)
mke2fs -q -F -t ext4 "${args[@]}" -O bigalloc,^orphan_file -C 65536 "$run/bigalloc.raw"
args=(-O ^orphan_file -b 4096 -N 1024)
mke2fs -q -F -t ext4 "${args[@]}" "$run/resize.raw" 128M
args=(-O ^orphan_file -b 4096 -N 4096)
mke2fs -q -F -t ext4 "${args[@]}" -O fast_commit,^orphan_file "$run/fc.raw"
mke2fs -q -F -t ext4 "${args[@]}" "$run/corrupt.raw"

# Corrupt one content byte inside the group-0 block and inode bitmaps while
# the image is offline.  The structure and the checksum trailer stay intact,
# so ext4 bitmap validation detects the change instead of a raw zeroed block.
read -r bitmap ibitmap <<EOF
$(dumpe2fs -g "$run/corrupt.raw" 2>/dev/null | awk -F: '$1 ~ /^[0-9]+$/ {print $5, $6; exit}')
EOF
test -n "$bitmap"; test -n "$ibitmap"
printf '\001' | dd of="$run/corrupt.raw" bs=1 seek=$((bitmap * 4096 + 17)) conv=notrunc status=none
printf '\001' | dd of="$run/corrupt.raw" bs=1 seek=$((ibitmap * 4096 + 17)) conv=notrunc status=none

opts=(-i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=3 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
scp_opts=(-i "$key" -P "$port" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

boot_guest()
{
  local boot=$1 log=$2
  qemu-system-x86_64 -machine pc,accel=tcg -cpu max -m 2048 -smp 2 \
    -kernel "$kernel" -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
    -drive "file=$run/bigalloc.raw,format=raw,if=virtio" \
    -drive "file=$run/resize.raw,format=raw,if=virtio" \
    -drive "file=$run/fc.raw,format=raw,if=virtio" \
    -drive "file=$run/corrupt.raw,format=raw,if=virtio" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=n0 \
    -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 selinux=0' \
    -display none -monitor none -serial "file:$run/$log" \
    -daemonize -pidfile "$run/qemu.pid"
  cat "$run/qemu.pid" > "$run/qemu-$boot.pid"
  for attempt in $(seq 1 240); do
    if ssh "${opts[@]}" root@127.0.0.1 'test -e /sys/fs/ext4/vda/counter_snapshot' 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  tail -60 "$run/$log" >&2
  return 1
}

stop_guest()
{
  local pid=$1
  kill "$pid" 2>/dev/null || true
  for attempt in $(seq 1 60); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
}

#############################################################################
# First boot: bigalloc rereserve, online resize, then leave the fast-commit
# image dirty and cut power instead of rebooting cleanly.
#############################################################################
boot_guest first serial-first.log
first_pid=$(<"$run/qemu-first.pid")
trap 'stop_guest "$first_pid" 2>/dev/null || true' EXIT
scp -q "${scp_opts[@]}" "$project/tests/guest-special.sh" root@127.0.0.1:/tmp/
scp -q "${scp_opts[@]}" "$fc_seed" root@127.0.0.1:/tmp/
if ! ssh "${opts[@]}" root@127.0.0.1 "FC_MODE=$fc_mode sh /tmp/guest-special.sh first" > "$run/guest-first.txt" 2>&1; then
  echo "guest-special first stage failed; see $run/guest-first.txt" >&2
  ssh "${opts[@]}" root@127.0.0.1 'dmesg | tail -60' > "$run/guest-first-dmesg.txt" 2>&1 || true
  exit 1
fi
test "$(cat "$run/guest-first.txt")" = fast_commit_armed
# The trace buffer does not survive the power cut, so copy the first boot's
# evidence out before killing the guest.
mkdir -p "$run/first-boot"
scp -qr "${scp_opts[@]}" root@127.0.0.1:/var/lib/ext4-counter-trace \
  "$run/first-boot/guest-evidence"
# Hard-stop the first VM without a guest shutdown or filesystem sync.
kill -9 "$first_pid" 2>/dev/null || true
sleep 1
trap - EXIT

#############################################################################
# Second boot: the pending fast commit is replayed during the vdd mount with
# tracing enabled, then the corrupted bitmaps are exercised.
#############################################################################
boot_guest second serial-second.log
second_pid=$(<"$run/qemu-second.pid")
trap 'stop_guest "$second_pid" 2>/dev/null || true' EXIT
scp -q "${scp_opts[@]}" "$project/tests/guest-special.sh" root@127.0.0.1:/tmp/
scp -q "${scp_opts[@]}" "$fc_seed" root@127.0.0.1:/tmp/
if ! ssh "${opts[@]}" root@127.0.0.1 "FC_MODE=$fc_mode sh /tmp/guest-special.sh second" > "$run/guest-second.txt" 2>&1; then
  echo "guest-special second stage failed; see $run/guest-second.txt" >&2
  exit 1
fi
scp -qr "${scp_opts[@]}" root@127.0.0.1:/var/lib/ext4-counter-trace "$run/"
if rg -q 'BUG:.*ext4|KASAN:.*(ext4|counter)|Oops:.*ext4|possible circular locking|WARNING:.*ext4' "$run/ext4-counter-trace/dmesg.txt"; then
  echo "Kernel diagnostic in $run/ext4-counter-trace/dmesg.txt" >&2
  exit 1
fi
ssh "${opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
stop_guest "$second_pid"
trap - EXIT
sha256sum "$kernel" "$project/build/linux-counter-trace/.config" > "$run/build.sha256"
# Preserve independent offline evidence for the replayed fast-commit image.
# This runs only after the guest has shut down cleanly.
if e2fsck -fn "$run/fc.raw" > "$run/fc-fsck.txt" 2>&1; then
  fc_fsck_rc=0
else
  fc_fsck_rc=$?
fi
if python3 "$project/scripts/check-disk.py" "$run/fc.raw" \
  --output "$run/fc-disk.json" > /dev/null; then
  fc_disk_rc=0
else
  fc_disk_rc=$?
fi
python3 - "$run" "$fc_fsck_rc" "$fc_disk_rc" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
def sha(name):
    h = hashlib.sha256()
    with (root / name).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()
report = {
    'fsck_exit': int(sys.argv[2]),
    'disk_checker_exit': int(sys.argv[3]),
    'sha256': {name: sha(name) for name in ('fc.raw', 'fc-fsck.txt', 'fc-disk.json')},
}
(root / 'fc-disk-evidence.json').write_text(json.dumps(report, indent=2) + '\n')
PY
python3 "$project/scripts/analyze-fc-dir-count.py" "$run" > "$run/fc-dir-count-finding.json"
printf 'counter_trace_special=fast_commit_dir_count_reproduced\nrun_dir=%s\n' "$run"
