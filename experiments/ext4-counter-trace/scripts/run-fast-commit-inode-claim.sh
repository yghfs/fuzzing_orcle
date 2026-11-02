#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "$project/../.." && pwd)
run=${EXT4_FAULT_FC_RUN_DIR:-$(mktemp -d "$project/results/fault-fast-commit-inode-claim-XXXXXXXX")}
port=${EXT4_FAULT_FC_PORT:-11942}
key="$root/work/debian.id_rsa"
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
root_base="$root/work/debian-clean-smalljournal.img"
fc_seed="$run/ext4-fast-commit-inode-replay"
mount_helper="$run/fault-fc-mount"
first_pid=
second_pid=

test -s "$kernel"
test -s "$root_base"
test -s "$key"
mkdir -p "$run/first-boot"
if [[ -n "${EXT4_FAULT_FC_RUN_DIR:-}" ]] && \
  find "$run" -mindepth 1 -maxdepth 1 ! -name first-boot -print -quit | grep -q .; then
  echo "run directory already contains files: $run" >&2
  exit 2
fi
python3 "$project/scripts/fault_plan.py" fast_commit_inode_claim \
  --output "$run/fault-plan.json"
gcc -O2 -Wall -Wextra -Werror -static -o "$fc_seed" \
  "$project/tests/special-fc-workload.c"
gcc -O2 -Wall -Wextra -Werror -static -o "$mount_helper" \
  "$project/tests/fault-fc-mount.c"
qemu-img create -q -f qcow2 -F raw -b "$root_base" "$run/root.qcow2"
qemu-img create -q -f raw "$run/fc.raw" 256M
mke2fs -q -F -t ext4 -b 4096 -N 4096 -O fast_commit,^orphan_file "$run/fc.raw"

ssh_guest()
{
  ssh -i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=3 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    root@127.0.0.1 "$@"
}

scp_guest()
{
  scp -i "$key" -P "$port" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$@"
}

stop_guest()
{
  local pid=$1
  [[ -n "$pid" ]] || return 0
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
}

cleanup()
{
  stop_guest "$second_pid"
  stop_guest "$first_pid"
}
trap cleanup EXIT

boot_guest()
{
  local label=$1 serial=$2 pid
  qemu-system-x86_64 -machine pc,accel=tcg -cpu max -m 2048 -smp 2 \
    -kernel "$kernel" \
    -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
    -drive "file=$run/fc.raw,format=raw,if=virtio" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=n0 \
    -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 selinux=0' \
    -display none -monitor none -serial "file:$run/$serial" \
    -daemonize -pidfile "$run/qemu-$label.pid"
  pid=$(<"$run/qemu-$label.pid")
  if [[ "$label" == first ]]; then first_pid=$pid; else second_pid=$pid; fi
  for _ in $(seq 1 240); do
    if ssh_guest 'test -e /sys/fs/ext4/vda/counter_snapshot' 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  tail -80 "$run/$serial" >&2
  return 1
}

printf 'FAULT FC PREPARE run_dir=%s\n' "$run"
boot_guest first serial-first.log
scp_guest "$fc_seed" "$project/tests/guest-fault-fc-inode-claim.sh" root@127.0.0.1:/tmp/
if ! ssh_guest 'sh /tmp/guest-fault-fc-inode-claim.sh first' \
  > "$run/guest-first.txt" 2>&1; then
  cat "$run/guest-first.txt" >&2
  tail -80 "$run/serial-first.log" >&2
  exit 1
fi
grep -qx 'fast_commit_armed' "$run/guest-first.txt"
scp_guest -qr root@127.0.0.1:/var/lib/ext4-counter-trace \
  "$run/first-boot/guest-evidence"

# Hard-stop the VM; a clean shutdown would commit the journal.
kill -9 "$first_pid" 2>/dev/null || true
first_pid=
for _ in $(seq 1 10); do
  kill -0 "$(cat "$run/qemu-first.pid")" 2>/dev/null || break
  sleep 1
done

set +e
dumpe2fs -h "$run/fc.raw" > "$run/fc-before-replay.txt" 2>&1
dump_rc=$?
set -e
if (( dump_rc != 0 )) || ! rg -q 'needs_recovery' "$run/fc-before-replay.txt"; then
  echo 'fast-commit image lacks pending journal recovery after power cut' >&2
  exit 1
fi
grep -q '^fast_commit_inode_claim_ready .*fresh_ino=' \
  "$run/first-boot/guest-evidence/fc-seed.txt"
printf 'FAULT FC POWER CUT pending_recovery=verified\n'

boot_guest second serial-second.log
scp_guest "$fc_seed" "$mount_helper" \
  "$project/tests/guest-fault-fc-inode-claim.sh" root@127.0.0.1:/tmp/
if ! ssh_guest 'sh /tmp/guest-fault-fc-inode-claim.sh second' \
  > "$run/guest-second.txt" 2>&1; then
  cat "$run/guest-second.txt" >&2
  tail -80 "$run/serial-second.log" >&2
  set +e
  scp_guest -qr root@127.0.0.1:/var/lib/ext4-counter-trace "$run/fault-run-partial"
  set -e
  exit 1
fi
scp_guest -qr root@127.0.0.1:/var/lib/ext4-counter-trace "$run/"
if rg -q 'BUG:.*ext4|KASAN:.*(ext4|counter)|Oops:.*ext4|possible circular locking|WARNING:.*ext4' \
  "$run/ext4-counter-trace/dmesg.txt"; then
  echo "kernel diagnostic in $run/ext4-counter-trace/dmesg.txt" >&2
  exit 1
fi
ssh_guest 'systemctl poweroff' >/dev/null 2>&1 || true
stop_guest "$second_pid"
second_pid=
trap - EXIT

set +e
e2fsck -fn "$run/fc.raw" > "$run/fc-fsck.txt" 2>&1
fsck_rc=$?
set -e
if python3 "$project/scripts/check-disk.py" "$run/fc.raw" \
  --output "$run/fc-disk.json" > /dev/null; then
  disk_rc=0
else
  disk_rc=$?
fi
python3 - "$run" "$fsck_rc" "$disk_rc" <<'PY'
import hashlib
import json
import pathlib
import sys

run = pathlib.Path(sys.argv[1])
def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

items = ("fc.raw", "fc-fsck.txt", "fc-disk.json")
report = {
    "fsck_exit": int(sys.argv[2]),
    "disk_checker_exit": int(sys.argv[3]),
    "sha256": {name: digest(run / name) for name in items},
}
(run / "fc-disk-evidence.json").write_text(json.dumps(report, indent=2) + "\n")
PY

set +e
python3 "$project/scripts/validate-fast-commit-inode-claim.py" "$run" \
  --output "$run/validation.json"
validate_rc=$?
set -e
printf 'fault_fast_commit_inode_claim=complete\nrun_dir=%s\n' "$run"
exit "$validate_rc"

