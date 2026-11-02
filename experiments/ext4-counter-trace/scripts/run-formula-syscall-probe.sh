#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
run=${EXT4_FORMULA_RUN_DIR:-$(mktemp -d "$project/results/formula-syscall-XXXXXXXX")}
port=${EXT4_FORMULA_PORT:-11902}
qemu_memory_mb=${EXT4_FORMULA_QEMU_MEMORY_MB:-1536}
trace_buffer_kb=${EXT4_FORMULA_TRACE_BUFFER_KB:-262144}
key="$root/work/debian.id_rsa"
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
test_image="$run/test.raw"
mkdir -p "$run"
test -s "$kernel"

gcc -O2 -Wall -Wextra -Werror -static -o "$run/formula-syscall-workload" \
	"$project/tests/formula-syscall-workload.c"
qemu-img create -q -f raw "$test_image" 200M
if [[ ${EXT4_EA_INODE:-0} == 1 ]]; then
	features='^orphan_file,ea_inode'
else
	features='^orphan_file,^ea_inode'
fi
mke2fs -q -F -t ext4 -O "$features" -b 4096 -N 16384 \
	-E lazy_itable_init=0 "$test_image" 40000
e2fsck -fn "$test_image" > "$run/baseline-fsck.txt" 2>&1
qemu-img create -q -f qcow2 -F raw -b "$root/work/debian-clean-smalljournal.img" \
	"$run/root.qcow2"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "$qemu_memory_mb" -smp 2 \
	-kernel "$kernel" \
	-drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=$test_image,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
	-display none -monitor none -serial "file:$run/serial.log" \
	-daemonize -pidfile "$run/qemu.pid"
pid=$(<"$run/qemu.pid")
trap 'kill "$pid" 2>/dev/null || true' EXIT

ssh_opts=(-i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
scp_opts=(-i "$key" -P "$port" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
ready=0
for _ in $(seq 1 180); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'test -r /etc/os-release' >/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 1
done
[[ $ready == 1 ]] || { tail -80 "$run/serial.log"; exit 1; }
scp -q "${scp_opts[@]}" "$run/formula-syscall-workload" root@127.0.0.1:/tmp/
scp -q "${scp_opts[@]}" "$project/tests/guest-formula-probe.sh" root@127.0.0.1:/tmp/
set +e
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"env EXT4_RESIZE_MEMORY_PRESSURE_MB=${EXT4_RESIZE_MEMORY_PRESSURE_MB:-0} EXT4_RESIZE_ONLY=${EXT4_RESIZE_ONLY:-0} EXT4_EA_ONLY=${EXT4_EA_ONLY:-0} EXT4_EA_INODE=${EXT4_EA_INODE:-0} EXT4_FORMULA_TRACE_BUFFER_KB=$trace_buffer_kb sh /tmp/guest-formula-probe.sh" \
	> "$run/runtime.txt" 2>&1
runtime_rc=$?
set -e
scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/formula-probe "$run/"
ssh "${ssh_opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
	kill -0 "$pid" 2>/dev/null || break
	sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
	echo 'guest did not power off cleanly; offline checks skipped' > "$run/fsck.txt"
	exit 1
fi
e2fsck -E journal_only -y "$test_image" > "$run/journal-replay.txt" 2>&1
set +e
e2fsck -fn "$test_image" > "$run/fsck.txt" 2>&1
fsck_rc=$?
set -e
python3 "$project/scripts/check-disk.py" "$test_image" --output "$run/disk.json" >/dev/null
python3 - "$run/formula-probe/before.json" "$run/formula-probe/after.json" "$run/disk.json" "$run" <<'PY'
import json, pathlib, sys
before, after, disk = (json.loads(pathlib.Path(p).read_text()) for p in sys.argv[1:4])
out = pathlib.Path(sys.argv[4])
fields = ("free_clusters", "free_inodes", "dirs")
summary = {"before": {k: before.get(k) for k in fields},
           "after": {k: after.get(k) for k in fields},
           "stable_valid": bool(after.get("valid") and after.get("namespace_valid")),
           "observed_mismatches": after.get("observed_mismatches"),
           "namespace_mismatched": after.get("namespace_mismatched"),
           "disk_status": disk.get("status"),
           "disk_bitmap_descriptor_match": disk.get("bitmap_descriptor_match"),
           "disk_superblock_descriptor_match": disk.get("superblock_descriptor_match"),
           "disk_r_ug": disk.get("r_ug"), "disk_r_link": disk.get("r_link"),
           "disk_unverified": disk.get("unverified")}
(out / "formula-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
PY
cp "$run/formula-probe"/{before.json,after.json,start.json,end.json,trace.txt,dmesg.txt} "$run/"
cp "$run/formula-probe/workload.txt" "$run/"
cp "$run/formula-probe"/cpu*-stats.txt "$run/"
rg -n 'mismatch|hardlink_failure|mkdir_full|inode_alloc_stop|resize_ioctl|workload_rc|valid' \
	"$run/trace.txt" "$run/workload.txt" "$run/formula-summary.json" > "$run/hits.txt" || true
printf 'formula_syscall_probe=complete\nrun_dir=%s\nruntime_rc=%s\nfsck_rc=%s\n' \
	"$run" "$runtime_rc" "$fsck_rc"
