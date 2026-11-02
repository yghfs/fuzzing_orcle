#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${ORPHAN_FAILED_WRITE_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
run_dir="${ORPHAN_FAILED_WRITE_RUN_DIR:-${work_dir}/ext4-orphan-file-failed-write-$(date +%Y%m%d%H%M%S)-$$}"
port="${ORPHAN_FAILED_WRITE_PORT:-12169}"
image_size="${ORPHAN_FAILED_WRITE_SIZE:-256M}"
ssh_key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

mkdir -p "${run_dir}"
test -r "${kernel}" || { echo "missing kernel: ${kernel}" >&2; exit 2; }
test -r "${root_base}" || { echo "missing root image: ${root_base}" >&2; exit 2; }
gcc -O2 -Wall -Wextra -Werror -static \
	-o "${run_dir}/failed-write" \
	"${root_dir}/tests/ext4-orphan-file-failed-write.c"
qemu-img create -q -f raw "${run_dir}/test.raw" "${image_size}"
mke2fs -q -F -t ext4 -O orphan_file,^64bit -b 4096 -N 65536 \
	-E lazy_itable_init=0 "${run_dir}/test.raw"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${run_dir}/test.raw" "${run_dir}/test.qcow2"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536" \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"
qemu_pid="$(<"${run_dir}/qemu.pid")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/orphan-failed; mountpoint -q /mnt/orphan-failed || mount -o nodelalloc /dev/vdb /mnt/orphan-failed; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then break; fi
	sleep 1
done
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${run_dir}/failed-write" \
	root@127.0.0.1:/tmp/failed-write
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'mkdir -p /mnt/orphan-failed/workload; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; /tmp/failed-write /mnt/orphan-failed/workload; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_check >/dev/null; cat /sys/kernel/debug/ext4/vdb/invariant_summary; dmesg | tail -200; sync; umount /mnt/orphan-failed; systemctl poweroff' \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 60); do
	kill -0 "${qemu_pid}" 2>/dev/null || break
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo 'guest did not stop cleanly' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" "${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' "${run_dir}" "${runtime_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'failed_write|workload_rc|model_orphan_file_entries|violations_total|EXT4 invariant violation|orphan' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
tail -25 "${run_dir}/fsck.txt"
test "${runtime_rc}" -eq 0
