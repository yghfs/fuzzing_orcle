#!/usr/bin/env bash
set -euo pipefail

study_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kernel="${KERNEL:-}"
initrd="${INITRD:-${study_dir}/build/source-fault-initramfs.cpio.gz}"
out_dir="${OUT_DIR:-${study_dir}/results/qemu}"
run_dir="${RUN_DIR:-/tmp/ext4-source-fault-run-$(date +%Y%m%d%H%M%S)-$$}"
case_filter="${CASE_FILTER:-}"

if [[ -z "${kernel}" || ! -r "${kernel}" ]]; then
	echo 'set KERNEL to the built Linux 6.12.1 bzImage' >&2
	exit 2
fi
if [[ ! -r "${initrd}" ]]; then
	"${study_dir}/scripts/build-initramfs.sh" "${initrd}"
fi
mkdir -p "${out_dir}" "${run_dir}"

run_case() {
	local name="$1" test="$2" op="$3" error="$4"
	local image_size=64M fs_blocks='' features='orphan_file'
	local image="${run_dir}/${name}.img"
	local log="${out_dir}/${name}-${error}.log"
	local append="console=ttyS0 panic=-1 loglevel=7 test=${test} op=${op} errno=${error}"
	if [[ -n "${case_filter}" && "${name}" != "${case_filter}" ]]; then
		return
	fi

	case "${test}" in
	group-extend)
		image_size=128M
		fs_blocks=16384
		;;
	group-add)
		image_size=256M
		fs_blocks=32768
		;;
	xattr-release)
		features='^orphan_file'
		;;
	esac
	truncate -s "${image_size}" "${image}"
	if [[ -n "${fs_blocks}" ]]; then
		mke2fs -q -F -t ext4 -b 4096 -O "${features}" "${image}" "${fs_blocks}"
	else
		mke2fs -q -F -t ext4 -b 4096 -O "${features}" "${image}"
	fi

	set +e
	timeout 120 qemu-system-x86_64 -machine q35,accel=tcg -m 512 -smp 2 \
		-nographic -no-reboot -kernel "${kernel}" -initrd "${initrd}" \
		-append "${append}" -drive "file=${image},if=virtio,format=raw" \
		>"${log}" 2>&1
	local qemu_rc=$?
	set -e
	dumpe2fs -h "${image}" >"${log%.log}.dumpe2fs.log" 2>&1 || true
	e2fsck -fn "${image}" >"${log%.log}.e2fsck.log" 2>&1 || true
	if [[ "${name}" == unlink-parent* ]]; then
		debugfs -R 'stat <13>' "${image}" \
			>"${log%.log}.debugfs-stat-13.txt" 2>&1 || true
		debugfs -R 'ls -l /' "${image}" \
			>"${log%.log}.debugfs-root.txt" 2>&1 || true
	fi
	printf '[%s] qemu_rc=%s image=%s\n' "${name}" "${qemu_rc}" "${image}"
	rg 'EXT4 source fault|Aborting journal on device|unable to read itable block|RESIZE_RESULT|WORKLOAD_RESULT|SOURCE_FAULT_RESULT|DIRLINK_RESULT|LINK_RESULT|RENAME_RESULT|UNMOUNT_RESULT|MOUNT_FAILED|Power down' "${log}" || true
	if [[ "${test}" == group-add ]]; then
		"${study_dir}/scripts/inspect-group-add.py" "${image}" \
			>"${log%.log}.raw-descriptor.txt"
		cat "${log%.log}.raw-descriptor.txt"
	fi
}

run_case mkdir-parent mkdir-parent mkdir_parent_inode_dirty EIO
run_case rmdir-child rmdir-child rmdir_child_inode_dirty ENOMEM
run_case unlink-parent unlink unlink_parent_inode_dirty EIO
run_case unlink-parent-reserve unlink-reserve unlink_parent_reserve EIO
run_case unlink-parent-global-abort unlink-global-abort unlink_parent_global_abort EIO
run_case unlink-parent-inode-read unlink-inode-read unlink_parent_inode_read EIO
run_case unlink-parent-precondition unlink-prepare off EIO
run_case rename-source rename rename_source_inode_dirty EIO
run_case evict-final evict-final evict_final_inode_dirty ENOMEM
run_case orphan-access orphan-access orphan_file_delete_access EIO
run_case xattr-release xattr-release xattr_release_access EIO
run_case group-extend group-extend group_extend_buddy_load EIO
run_case group-add group-add group_add_groupinfo ENOMEM
