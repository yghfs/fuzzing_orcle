#!/usr/bin/env bash
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
run=$(mktemp -d "$project/results/chain-XXXXXXXX")
port=${EXT4_CT_CHAIN_PORT:-11882}
key=${EXT4_CT_SSH_KEY:-"$root/work/debian.id_rsa"}
rootfs=${EXT4_CT_ROOTFS:-}
if [[ -z "$rootfs" ]]; then
	for candidate in "$root/work/debian.img" "$root/work/debian-clean-smalljournal.img"; do
		if [[ -s "$candidate" ]]; then rootfs=$candidate; break; fi
	done
fi
test -s "$rootfs" || { echo "guest root image missing; run tools/create-vm-image.sh: $rootfs" >&2; exit 1; }
test -r "$key" || { echo "guest SSH key missing; run tools/create-vm-image.sh: $key" >&2; exit 1; }
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
test -s "$kernel"
mkdir -p "$run/evidence/scripts" "$run/evidence/tests"
cp "$kernel" "$run/kernel.bzImage"
cp "$project/build/linux-counter-trace/.config" "$run/evidence/kernel.config"
cp "$project/patches/0001-counter-trace-core-and-probes.patch" "$project/patches/manifest.json" "$run/evidence/"
cp "$project/scripts/trace_lifecycle.py" "$project/scripts/analyze-lifecycle.py" \
  "$project/scripts/make-chain-manifest.py" "$project/scripts/check-disk.py" \
  "$project/scripts/run-chain.sh" "$run/evidence/scripts/"
cp "$project/tests/chain-workload.c" "$project/tests/guest-chain.sh" "$run/evidence/tests/"
kernel="$run/kernel.bzImage"
gcc -O2 -Wall -Wextra -Werror -static -o "$run/ct-chain-workload" "$project/tests/chain-workload.c"
qemu-img create -q -f qcow2 -F raw -b "$rootfs" "$run/root.qcow2"
for kind in regular bigalloc inline; do
  size=132M
  qemu-img create -q -f raw "$run/$kind.raw" "$size"
  args=(-O ^orphan_file -b 4096 -N 4096 -E lazy_itable_init=0)
  [[ $kind != bigalloc ]] || args+=(-O bigalloc,^orphan_file -C 65536)
  [[ $kind != inline ]] || args+=(-O inline_data,^orphan_file)
  mke2fs -q -F -t ext4 "${args[@]}" "$run/$kind.raw"
done
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 2048 -smp 2 \
  -kernel "$kernel" -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
  -drive "file=$run/regular.raw,format=raw,if=virtio" \
  -drive "file=$run/bigalloc.raw,format=raw,if=virtio" \
  -drive "file=$run/inline.raw,format=raw,if=virtio" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
  -device virtio-net-pci,netdev=n0 -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
  -display none -monitor none -serial "file:$run/serial.log" -daemonize -pidfile "$run/qemu.pid"
pid=$(<"$run/qemu.pid")
trap 'kill "$pid" 2>/dev/null || true' EXIT
opts=(-i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=3 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
ready=0
for attempt in $(seq 1 180); do
  if ssh "${opts[@]}" root@127.0.0.1 'test -e /sys/fs/ext4/vda/counter_snapshot' 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ $ready == 1 ]] || { tail -60 "$run/serial.log"; exit 1; }
scp_opts=(-i "$key" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
scp -q "${scp_opts[@]}" "$run/ct-chain-workload" "$project/tests/guest-chain.sh" root@127.0.0.1:/tmp/
ssh "${opts[@]}" root@127.0.0.1 'sh /tmp/guest-chain.sh' > "$run/guest.txt" 2>&1 || {
  scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/ct-results "$run/" || true
  exit 1
}
scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/ct-results "$run/"
if rg -q 'BUG:|KASAN:|Oops:|possible circular locking|WARNING:' "$run/ct-results/dmesg.txt"; then
  echo "Kernel diagnostic in $run/ct-results/dmesg.txt" >&2
  exit 1
fi
ssh "${opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
for attempt in $(seq 1 60); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
  echo "Guest did not power off; refusing offline fsck: $run" >&2
  exit 1
fi
for kind in regular bigalloc inline; do
  e2fsck -fn "$run/$kind.raw" > "$run/$kind-fsck.txt" 2>&1
done
for kind in regular bigalloc inline; do
  python3 "$project/scripts/check-disk.py" "$run/$kind.raw" \
    --output "$run/$kind-disk.json" > /dev/null
  cp "$run/$kind-disk.json" "$run/ct-results/$kind-disk.json"
done
python3 "$project/scripts/make-chain-manifest.py" "$run/ct-results"
python3 "$project/scripts/analyze-lifecycle.py" "$run/ct-results" > "$run/analysis.json"
sha256sum "$kernel" "$run/evidence/kernel.config" > "$run/build.sha256"
printf 'counter_trace_chain=passed\nrun_dir=%s\n' "$run"
