#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
build_dir="${EXT4_INDIRECT_BUILD_DIR:-${work_dir}/ext4-indirect-kbuild}"
kernel="${EXT4_INDIRECT_KERNEL:-${work_dir}/kernels/ext4-indirect-count-pressure/bzImage}"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
port="${EXT4_INDIRECT_PORT:-12793}"
branches="${EXT4_INDIRECT_BRANCHES:-192}"
pressure_mib="${EXT4_INDIRECT_PRESSURE_MIB:-180}"
dense_mib="${EXT4_INDIRECT_DENSE_MIB:-0}"
data_size="${EXT4_INDIRECT_DATA_SIZE:-384M}"
blocks_per_group="${EXT4_INDIRECT_BLOCKS_PER_GROUP:-1024}"
vm_mib="${EXT4_INDIRECT_VM_MIB:-512}"
run_dir="${EXT4_INDIRECT_RUN_DIR:-${work_dir}/ext4-indirect-count-pressure-$(date +%Y%m%d%H%M%S)-$$}"

mkdir -p "${run_dir}" "$(dirname "${kernel}")"
if [[ ! -e "${build_dir}/.config" ]]; then
	mkdir -p "${build_dir}"
	cp "${work_dir}/kernels/ext4-count-normal-new/config" "${build_dir}/.config"
	"${root_dir}/kernel/linux-6.12.1/scripts/config" \
		--file "${build_dir}/.config" -d KASAN -d WERROR
	make -C "${root_dir}/kernel/linux-6.12.1" O="${build_dir}" olddefconfig
fi
grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "${build_dir}/.config"
if grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${build_dir}/.config"; then
	echo 'fault injection enabled in discovery kernel' >&2
	exit 2
fi
if grep -Eq '^CONFIG_KASAN=y$' "${build_dir}/.config"; then
	echo 'KASAN kernel is too memory intensive for this pressure workload' >&2
	exit 2
fi
gcc -O2 -Wall -Wextra -Werror -static \
	-o "${run_dir}/workload" \
	"${root_dir}/tests/ext4-indirect-count-pressure.c"

make -C "${root_dir}/kernel/linux-6.12.1" O="${build_dir}" -j"$(nproc)" bzImage
cp "${build_dir}/arch/x86/boot/bzImage" "${kernel}"
qemu-img create -q -f qcow2 -F raw -b "${base_image}" "${run_dir}/root.qcow2"
qemu-img create -q -f raw "${run_dir}/data.raw" "${data_size}"
mke2fs -q -F -t ext4 -b 4096 -N 8192 \
	-O '^64bit,^extent,^ea_inode,^orphan_file' \
	-g "${blocks_per_group}" -J size=4 -E lazy_itable_init=0 "${run_dir}/data.raw"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m "${vm_mib}" -smp 2 \
	-kernel "${kernel}" \
	-drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/data.raw,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor "unix:${run_dir}/qemu-monitor.sock,server=on,wait=off" \
	-serial "file:${run_dir}/serial.log" \
	-daemonize -pidfile "${run_dir}/qemu.pid"

qemu_pid="$(<"${run_dir}/qemu.pid")"
ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes \
	-o ConnectTimeout=4 -o StrictHostKeyChecking=no \
	-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
ready=0
for _ in $(seq 1 120); do
	if ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'mkdir -p /mnt/indirect-pressure; mountpoint -q /mnt/indirect-pressure || mount /dev/vdb /mnt/indirect-pressure; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' \
		>/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 1
done
if [[ "${ready}" != 1 ]]; then
	echo "guest SSH did not become ready; requesting ACPI powerdown: ${run_dir}" >&2
	printf 'system_powerdown\n' | nc -U -w 2 -q 0 "${run_dir}/qemu-monitor.sock" || true
	for _ in $(seq 1 120); do
		if ! kill -0 "${qemu_pid}" 2>/dev/null; then
			break
		fi
		sleep 1
	done
	if kill -0 "${qemu_pid}" 2>/dev/null; then
		echo "guest did not complete ACPI powerdown; leaving VM running: pid=${qemu_pid}" >&2
	fi
	exit 1
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	'test -r /sys/kernel/debug/ext4/vdb/invariant_summary && test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	"${run_dir}/workload" root@127.0.0.1:/tmp/indirect-pressure

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"set -u; printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; \
	mkdir -p /mnt/indirect-pressure; mountpoint -q /mnt/indirect-pressure || mount /dev/vdb /mnt/indirect-pressure; \
	echo 3 > /proc/sys/vm/drop_caches; \
	/tmp/indirect-pressure /mnt/indirect-pressure/file '${branches}' '${pressure_mib}' '${dense_mib}'; workload_rc=\$?; \
	echo workload_rc=\$workload_rc; sync; \
	cat /sys/kernel/debug/ext4/vdb/invariant_summary; \
	cat /sys/kernel/debug/ext4/vdb/invariant_check; \
	dmesg | grep -E 'model=extent_blocks|Read failure|EXT4-fs error|Out of memory|Killed process' | tail -160; \
	umount /mnt/indirect-pressure; systemctl poweroff" \
	>"${run_dir}/runtime.txt" 2>&1
runtime_rc=$?
set -e

for _ in $(seq 1 120); do
	if ! kill -0 "${qemu_pid}" 2>/dev/null; then
		break
	fi
	sleep 1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
	echo "guest did not complete clean poweroff; leaving VM running: pid=${qemu_pid} run_dir=${run_dir}" >&2
	exit 1
fi
set +e
e2fsck -fn "${run_dir}/data.raw" >"${run_dir}/e2fsck.txt" 2>&1
fsck_rc=$?
set -e
printf 'run_dir=%s runtime_rc=%s fsck_rc=%s\n' \
	"${run_dir}" "${runtime_rc}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'prepared|truncate_rc|workload_rc|model_extent_blocks|violations_total|snapshot OK|Read failure|Block bitmap differences|Free .* count|i_blocks' \
	"${run_dir}/runtime.txt" "${run_dir}/e2fsck.txt" || true
