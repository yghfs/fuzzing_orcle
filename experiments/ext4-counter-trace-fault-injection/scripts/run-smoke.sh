#!/usr/bin/env bash
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
port=${EXT4_EFI_PORT:-11872}
key=${EXT4_EFI_SSH_KEY:-"$root/work/debian.id_rsa"}
rootfs=${EXT4_EFI_ROOTFS:-"$root/work/debian-normal.img"}
kernel_obj="$project/build/linux-error-site-trace"
kernel="$kernel_obj/arch/x86/boot/bzImage"
test -s "$kernel" && test -s "$rootfs" && test -r "$key"
python3 "$project/scripts/instrument-sites.py" --check
python3 "$project/scripts/configure-injection.py" --check --prepare-source
python3 - "$project" "$kernel_obj" <<'PY'
import hashlib, json, sys
from pathlib import Path
project, out = map(Path, sys.argv[1:])
paths = {'sites': project/'sites.json', 'patch': project/'patches/0001-reviewed-error-site-trace.patch',
         'injection': project/'injection.json', 'injection_patch': project/'patches/0002-formula-gated-injection.patch',
         'base_patch': project/'../ext4-counter-trace/patches/0001-counter-trace-core-and-probes.patch',
         'source_audit': out/'source-audit.json',
         'config': out/'.config', 'kernel': out/'arch/x86/boot/bzImage'}
binding = json.loads((out/'build-binding.json').read_text())
for key, path in paths.items():
    assert binding[key] == hashlib.sha256(path.read_bytes()).hexdigest(), f'build binding changed: {key}'
PY
mkdir -p "$project/results"
run=$(mktemp -d "$project/results/error-sites-XXXXXXXX")
printf 'run_dir=%s\n' "$run"
cp "$kernel" "$run/kernel.bzImage"
cp "$kernel_obj/.config" "$run/kernel.config"
cp "$kernel_obj/build-binding.json" "$kernel_obj/source-audit.json" "$project/sites.json" "$run/"
cp "$project/tests/error-site-workload.c" "$project/tests/guest-error-sites.sh" "$run/"
gcc -O2 -Wall -Wextra -Werror -static -o "$run/error-site-workload" "$run/error-site-workload.c"
qemu-img create -q -f qcow2 -F raw -b "$rootfs" "$run/root.qcow2"
for kind in regular inline; do
  truncate -s 132M "$run/$kind.raw"
  options=(-O ^orphan_file -b 4096 -N 4096 -E lazy_itable_init=0)
  [[ $kind != inline ]] || options+=(-O inline_data,^orphan_file -I 512)
  mke2fs -q -F -t ext4 "${options[@]}" "$run/$kind.raw"
done
qemu-system-x86_64 -machine q35,accel=tcg -cpu qemu64 -m 2048 -smp 2 \
  -kernel "$run/kernel.bzImage" -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
  -drive "file=$run/regular.raw,format=raw,if=virtio" \
  -drive "file=$run/inline.raw,format=raw,if=virtio" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=n0 \
  -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
  -display none -monitor none -serial "file:$run/serial.log" -daemonize -pidfile "$run/qemu.pid"
pid=$(<"$run/qemu.pid")
trap 'kill "$pid" 2>/dev/null || true' EXIT
opts=(-i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=2 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
deadline=$((SECONDS + 240))
ready=0
while (( SECONDS < deadline )); do
  if ssh "${opts[@]}" root@127.0.0.1 'test -e /sys/kernel/debug/ext4_error_sites/stats || mount -t debugfs debugfs /sys/kernel/debug && test -e /sys/kernel/debug/ext4_error_sites/stats' 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ $ready == 1 ]] || { tail -60 "$run/serial.log"; exit 1; }
scp_opts=(-i "$key" -P "$port" -o BatchMode=yes -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
scp -q "${scp_opts[@]}" "$run/error-site-workload" "$run/guest-error-sites.sh" root@127.0.0.1:/tmp/
guest_status=0
timeout 600 ssh "${opts[@]}" root@127.0.0.1 'sh /tmp/guest-error-sites.sh' > "$run/guest.txt" 2>&1 || guest_status=$?
scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/error-site-results "$run/" || true
[[ $guest_status == 0 ]] || { cat "$run/guest.txt"; exit "$guest_status"; }
ssh "${opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
  echo "Guest did not power off; refusing offline fsck: $run" >&2
  exit 1
fi
for kind in regular inline; do
  e2fsck -fn "$run/$kind.raw" > "$run/$kind-fsck.txt" 2>&1
done
python3 "$project/tests/verify-smoke.py" "$run"
for kind in regular inline; do
  for phase in mount workload; do
    python3 "$project/scripts/report-sites.py" "$run/error-site-results/$kind-$phase.trace" \
      --manifest "$run/sites.json" --output "$run/$kind-$phase-report" > "$run/$kind-$phase-summary.txt"
  done
done
printf 'error_site_smoke=passed\nrun_dir=%s\n' "$run"
