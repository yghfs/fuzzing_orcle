#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_INDIRECT_SPLICE_KERNEL:-${work_dir}/ext4-new-kbuild/arch/x86/boot/bzImage}"
config="${EXT4_INDIRECT_SPLICE_CONFIG:-${work_dir}/ext4-new-kbuild/.config}"
root_base="${EXT4_INDIRECT_SPLICE_ROOT_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_INDIRECT_SPLICE_PORT:-11967}"
vm_mib="${EXT4_INDIRECT_SPLICE_VM_MIB:-512}"
limits="${EXT4_INDIRECT_SPLICE_LIMITS:-4096,8192,12288,16384,24576,32768,49152,65536,98304}"
run_dir="${EXT4_INDIRECT_SPLICE_RUN_DIR:-${work_dir}/ext4-indirect-splice-natural-enomem-$(date +%Y%m%d%H%M%S)-$$}"
key="${work_dir}/debian.id_rsa"
ssh_opts=(-i "${key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${config}"
grep -qx '# CONFIG_EXT4_FAULT_INJECTION is not set' "${config}"
[[ -r "${kernel}" && -r "${root_base}" ]]
mkdir -p "${run_dir}"
program="${run_dir}/ext4-indirect-splice-natural-enomem"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" \
	"${root_dir}/tests/ext4-indirect-splice-natural-enomem.c"
test_image="${run_dir}/test.raw"
qemu-img create -q -f raw "${test_image}" 256M
mke2fs -q -F -t ext4 -b 4096 -N 8192 \
	-O '^64bit,^extent,^ea_inode,^orphan_file' -J size=4 \
	-E lazy_itable_init=0 "${test_image}"
e2fsck -fn "${test_image}" >"${run_dir}/baseline-fsck.txt" 2>&1 || true
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu_pid_file="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${vm_mib}M" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${test_image},format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=1048576' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${qemu_pid_file}"
qemu_pid="$(<"${qemu_pid_file}")"
cleanup() { kill "${qemu_pid}" 2>/dev/null || true; }
trap cleanup EXIT

ready=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/ext4-indirect-splice; mountpoint -q /mnt/ext4-indirect-splice || mount -o nodelalloc /dev/vdb /mnt/ext4-indirect-splice; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 1
done
if [[ "${ready}" != 1 ]]; then
	echo 'guest did not mount checker filesystem' >&2
	exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 'test ! -e /sys/module/ext4/parameters/ext4_fault_mode; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; echo 3 > /proc/sys/vm/drop_caches'
scp -q -i "${key}" -P "${port}" -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/
set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"timeout --foreground 300s /tmp/ext4-indirect-splice-natural-enomem /mnt/ext4-indirect-splice '${limits}'; rc=\$?; echo workload_rc=\${rc}; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check; dmesg | grep -E 'extent_blocks|indirect|reachable|Out of memory|Killed process|EXT4-fs.*error|WARNING:|BUG:' | tail -240; sync; umount /mnt/ext4-indirect-splice; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e
for _ in $(seq 1 90); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	fsck_rc=125
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
else
	set +e
	e2fsck -fn "${test_image}" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s limits=%s\n' \
	"${run_dir}" "${runtime_rc}" "${fsck_rc}" "${limits}" | tee "${run_dir}/report.txt"
rg -n 'attempt=|pwrite|workload_rc|model_extent_blocks|model=|violations_total|snapshot_races|events_dropped|Block bitmap differences|Free .* count|Inode bitmap differences|Filesystem still has errors' \
	"${run_dir}/runtime.txt" "${run_dir}/fsck.txt" | tee -a "${run_dir}/report.txt" || true
