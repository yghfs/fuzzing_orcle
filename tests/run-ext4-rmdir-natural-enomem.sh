#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
study_dir="${root_dir}/experiments/ext4-source-fault-study"
kernel="${KERNEL:-${root_dir}/work/ext4-semantic-build-1793467124/arch/x86/boot/bzImage}"
run_dir="${RUN_DIR:-${root_dir}/work/ext4-rmdir-natural-enomem-$(date +%Y%m%d%H%M%S)-$$}"
out_dir="${OUT_DIR:-${study_dir}/results/qemu-natural-rmdir-enomem-$(date +%Y%m%d%H%M%S)}"
children="${CHILDREN:-2048}"
cases="${CASES:-128:fd:300000 96:fd:150000 80:fd:100000}"

if [[ ! -r "${kernel}" ]]; then
	echo "set KERNEL to a Linux x86-64 bzImage" >&2
	exit 2
fi
mkdir -p "${run_dir}" "${out_dir}"
cp -a "${study_dir}/initramfs/." "${run_dir}/initramfs"
gcc -O2 -Wall -Wextra -Werror -static \
	-o "${run_dir}/initramfs/bin/rmdir-natural" \
	"${root_dir}/tests/ext4-rmdir-natural-enomem.c"
cat >"${run_dir}/initramfs/init" <<'EOF'
#!/bin/busybox sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /mnt /pressure
CHILDREN=256
PRESSURE_KIND=anon
PRESSURE_AMOUNT=64
for arg in $(cat /proc/cmdline); do
	case "$arg" in
		children=*) CHILDREN=${arg#children=} ;;
		pressure_kind=*) PRESSURE_KIND=${arg#pressure_kind=} ;;
		pressure_amount=*) PRESSURE_AMOUNT=${arg#pressure_amount=} ;;
	esac
done
mount -t ext4 -o errors=continue /dev/vda /mnt || {
	echo MOUNT_FAILED
	exec /bin/busybox poweroff -f
}
/bin/rmdir-natural prepare "$CHILDREN" || exec /bin/busybox poweroff -f
sync
echo 3 > /proc/sys/vm/drop_caches
echo "MEMORY_BEFORE_PRESSURE"
cat /proc/meminfo
set +e
/bin/rmdir-natural run "$CHILDREN" "$PRESSURE_KIND" "$PRESSURE_AMOUNT"
WORKLOAD_RC=$?
set -e
echo "WORKLOAD_EXIT rc=$WORKLOAD_RC"
sync
umount /mnt
echo "UNMOUNT_RESULT rc=$?"
exec /bin/busybox poweroff -f
EOF
chmod 0755 "${run_dir}/initramfs/init"
(
	cd "${run_dir}/initramfs"
	find . -print0 | cpio --null -o -H newc --quiet | gzip -1
) >"${run_dir}/rmdir-natural-initramfs.cpio.gz"

for spec in ${cases}; do
	IFS=: read -r ram_mib pressure_kind pressure_amount <<<"${spec}"
	case_name="ram${ram_mib}-${pressure_kind}${pressure_amount}-children${children}"
	image="${run_dir}/${case_name}.img"
	log="${out_dir}/${case_name}.log"
	truncate -s 96M "${image}"
	mke2fs -q -F -t ext4 -b 4096 "${image}"
	set +e
	timeout 180 qemu-system-x86_64 -machine q35,accel=tcg -m "${ram_mib}" -smp 1 \
		-nographic -no-reboot -kernel "${kernel}" \
		-initrd "${run_dir}/rmdir-natural-initramfs.cpio.gz" \
		-append "console=ttyS0 panic=-1 loglevel=7 children=${children} pressure_kind=${pressure_kind} pressure_amount=${pressure_amount}" \
		-drive "file=${image},if=virtio,format=raw" >"${log}" 2>&1
	qemu_rc=$?
	set -e
	e2fsck -fn "${image}" >"${log%.log}.e2fsck.log" 2>&1 || true
	debugfs -R 'stat /p' "${image}" >"${log%.log}.debugfs-parent.txt" 2>&1 || true
	printf 'CASE_RESULT %s qemu_rc=%s image=%s\n' "${case_name}" "${qemu_rc}" "${image}"
	rg 'PREPARE_RESULT|PRESSURE_RESULT|RMDIR_ENOMEM|RMDIR_PRESSURE_RESULT|ext4_rmdir|Out of memory|Killed process|UNMOUNT_RESULT|Power down' "${log}" || true
	cat "${log%.log}.e2fsck.log"
done
printf 'RESULT_DIR %s\n' "${out_dir}"
