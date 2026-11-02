#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_EVICT_UMOUNT_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
config="${EXT4_EVICT_UMOUNT_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_EVICT_UMOUNT_ROOT:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_EVICT_UMOUNT_PORT:-11983}"
run_dir="${EXT4_EVICT_UMOUNT_RUN_DIR:-${work_dir}/ext4-evict-umount-natural-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${config}"
grep -q '^# CONFIG_EXT4_FAULT_INJECTION is not set$' "${config}"
mkdir -p "${run_dir}"
gcc -O2 -Wall -Wextra -Werror -static -o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-evict-umount-natural.c"
qemu-img create -q -f raw "${run_dir}/test.raw" 256M
mke2fs -q -F -t ext4 -O orphan_file -b 4096 -N 65536 -J size=4 \
	-E lazy_itable_init=0 "${run_dir}/test.raw"
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 768M -smp 2 -kernel "${kernel}" \
	-drive file="${run_dir}/root.qcow2",format=qcow2,if=virtio \
	-drive file="${run_dir}/test.raw",format=raw,if=virtio \
	-netdev user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22 -device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial file:"${run_dir}/serial.log" -daemonize -pidfile "${run_dir}/qemu.pid"
Q=$(<"${run_dir}/qemu.pid"); trap 'kill "${Q}" 2>/dev/null || true' EXIT
for _ in $(seq 1 120); do
	ssh "${opts[@]}" root@127.0.0.1 'mkdir -p /mnt/ext4-evict-umount; mountpoint -q /mnt/ext4-evict-umount || mount /dev/vdb /mnt/ext4-evict-umount; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1 && break
	sleep 1
done
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/workload" root@127.0.0.1:/tmp/ext4-evict-umount
ssh "${opts[@]}" root@127.0.0.1 'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; /tmp/ext4-evict-umount /mnt/ext4-evict-umount; rc=$?; echo workload_rc=$rc; cat /sys/kernel/debug/ext4/vdb/invariant_summary; sync; umount /mnt/ext4-evict-umount; mount /dev/vdb /mnt/ext4-evict-umount; echo remounted; ls -l /mnt/ext4-evict-umount; umount /mnt/ext4-evict-umount; systemctl poweroff' >"${run_dir}/runtime.txt" 2>&1 || true
for _ in $(seq 1 45); do kill -0 "${Q}" 2>/dev/null || break; sleep 1; done
set +e; e2fsck -fn "${run_dir}/test.raw" >"${run_dir}/fsck.txt" 2>&1; FS=$?; set -e
printf 'run_dir=%s fsck_rc=%s\n' "${run_dir}" "${FS}" | tee "${run_dir}/report.txt"
rg -n 'workload_rc|model_|violations_total|last_violation|Inode bitmap|Free inodes|remounted' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
grep -Eq '^model_inode_bitmap_violations=[1-9][0-9]*$' "${run_dir}/runtime.txt"
