#!/usr/bin/env bash
# Shared inputs for fixed workloads that require the consistency checker.
source "$(dirname "${BASH_SOURCE[0]}")/../tools/kernel-paths.sh"

ext4_normal_guest_image="$FUZZING_ORCLE_ROOTFS"

ext4_require_normal_kernel() {
	local kernel="$1" config="${2:-${EXT4_NORMAL_KERNEL_CONFIG:-}}"
	if [[ -z "${config}" ]]; then
		case "${kernel}" in
			*/arch/x86/boot/bzImage) config="${kernel%/arch/x86/boot/bzImage}/.config" ;;
			*) config="$(dirname -- "${kernel}")/config" ;;
		esac
	fi
	if [[ ! -r "${kernel}" ]]; then
		printf 'Kernel missing: %s. Run experiments/ext4-counter-trace/scripts/build.sh --normal.\n' "${kernel}" >&2
		return 2
	fi
	if [[ ! -r "${config}" ]]; then
		printf 'Kernel config missing: %s. Set EXT4_NORMAL_KERNEL_CONFIG for a custom kernel.\n' "${config}" >&2
		return 2
	fi
	if ! rg -qx 'CONFIG_EXT4_COUNTER_TRACE=y' "${config}"; then
		printf 'Kernel config must enable the consistency checker: %s\n' "${config}" >&2
		return 2
	fi
}
