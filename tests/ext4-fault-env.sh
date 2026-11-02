#!/usr/bin/env bash
# Shared setup for candidate validation runners.
ext4_fault_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT4_FAULT_KERNEL="${EXT4_FAULT_KERNEL:-${ext4_fault_root}/work/kernels/ext4-fault/bzImage}"
if [[ "${EXT4_NATURAL_ONLY:-0}" == 1 ]]; then
	EXT4_FAULT_KERNEL="${EXT4_NATURAL_KERNEL:-${ext4_fault_root}/work/kernels/ext4-count-normal-new/bzImage}"
fi
if [[ ! -r "${EXT4_FAULT_KERNEL}" ]]; then
	echo "Candidate validation kernel missing: ${EXT4_FAULT_KERNEL}" >&2
	echo "Build with CONFIG_EXT4_FAULT_INJECTION=y; see tests/README.md." >&2
	exit 2
fi
if [[ -n "${EXT4_CHECKER_FAULT:-}" ]]; then
	echo "EXT4_CHECKER_FAULT was removed; use EXT4_FAULT_MODE for candidate validation." >&2
	exit 2
fi
echo "candidate validation kernel: ${EXT4_FAULT_KERNEL}" >&2
