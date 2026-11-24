#!/usr/bin/env bash
# Source this file for the normal regression kernel paths.
FUZZING_ORCLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_fuzzing_orcle_counter="$FUZZING_ORCLE_ROOT/experiments/ext4-counter-trace"
export FUZZING_ORCLE_ROOT
export FUZZING_ORCLE_KERNEL="${FUZZING_ORCLE_KERNEL:-$_fuzzing_orcle_counter/dev/linux-6.12.1}"
export FUZZING_ORCLE_KERNEL_OBJ="${FUZZING_ORCLE_KERNEL_OBJ:-$_fuzzing_orcle_counter/build/linux-counter-trace-normal}"
export FUZZING_ORCLE_BZIMAGE="${FUZZING_ORCLE_BZIMAGE:-$FUZZING_ORCLE_KERNEL_OBJ/arch/x86/boot/bzImage}"
export FUZZING_ORCLE_ROOTFS="${FUZZING_ORCLE_ROOTFS:-$FUZZING_ORCLE_ROOT/work/debian-normal.img}"
unset _fuzzing_orcle_counter
