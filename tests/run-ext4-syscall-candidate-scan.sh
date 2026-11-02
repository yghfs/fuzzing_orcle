#!/usr/bin/env bash
set -euo pipefail

# Ordinary-syscall discovery runner. This intentionally requires a kernel
# without CONFIG_EXT4_FAULT_INJECTION; fault injection belongs to follow-up
# confirmation, never to candidate discovery.
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${SYSCALL_SCAN_KERNEL:-${work_dir}/kernels/ext4-count-normal/bzImage}"
kernel_config="${SYSCALL_SCAN_KERNEL_CONFIG:-$(dirname "${kernel_image}")/config}"
base_image="${SYSCALL_SCAN_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
config_file="${SYSCALL_SCAN_CONFIG:-${root_dir}/tools/ext4-full-manager.cfg}"
kernel_obj="${SYSCALL_SCAN_KERNEL_OBJ:-${root_dir}/kernel/linux-6.12.1}"
run_dir="${SYSCALL_SCAN_RUN_DIR:-${work_dir}/ext4-syscall-scan-$(date +%Y%m%d%H%M%S)-$$}"
duration_s="${SYSCALL_SCAN_DURATION:-300}"
http_port="${SYSCALL_SCAN_HTTP_PORT:-56844}"
ssh_port="${SYSCALL_SCAN_SSH_PORT:-12144}"
event_cap="${SYSCALL_SCAN_EVENT_CAP:-65536}"

mkdir -p "${run_dir}"
test -x "${root_dir}/tools/syzkaller/bin/syz-manager" || {
  echo "missing syz-manager" >&2
  exit 2
}
test -r "${kernel_image}" || { echo "missing kernel: ${kernel_image}" >&2; exit 2; }
test -r "${kernel_config}" || { echo "missing kernel config: ${kernel_config}" >&2; exit 2; }
test -r "${base_image}" || { echo "missing image: ${base_image}" >&2; exit 2; }

if grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
  echo "test kernel config has ext4 fault injection enabled; refusing discovery run" >&2
  exit 2
fi
if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${kernel_config}"; then
  echo "test kernel config lacks CONFIG_EXT4_INVARIANT_CHECKER=y" >&2
  exit 2
fi

port_cfg="${run_dir}/manager.json"
sed -e "s#\"http\": \"127.0.0.1:[0-9]*\"#\"http\": \"127.0.0.1:${http_port}\"#" \
    -e "s#\"workdir\": \"[^\"]*\"#\"workdir\": \"${run_dir}/syz-work\"#" \
    -e "s#\"kernel_obj\": \"[^\"]*\"#\"kernel_obj\": \"${kernel_obj}\"#" \
    -e "s#\"image\": \"[^\"]*\"#\"image\": \"${base_image}\"#" \
    -e "s#\"kernel\": \"[^\"]*\"#\"kernel\": \"${kernel_image}\"#" \
    -e "s#\"procs\": [0-9]*#\"procs\": ${SYSCALL_SCAN_PROCS:-4}#" \
    -e "s#ext4_checker_event_cap=[0-9]*#ext4_checker_event_cap=${event_cap}#" \
    "${config_file}" > "${port_cfg}"

manager_log="${run_dir}/manager.log"
monitor_log="${run_dir}/monitor.log"
manager_bin="${root_dir}/tools/syzkaller/bin/syz-manager"
ssh_key="${work_dir}/debian.id_rsa"

"${manager_bin}" -config="${port_cfg}" >"${manager_log}" 2>&1 &
manager_pid=$!
cleanup() { kill "${manager_pid}" 2>/dev/null || true; }
trap cleanup EXIT

start="$(date +%s)"
end=$((start + duration_s))
models=(inode_bitmap block_bitmap reservation directory_link_count mkdir_atomicity)
declare -A last_model_violations=( [inode_bitmap]=0 [block_bitmap]=0 [reservation]=0 [directory_link_count]=0 [mkdir_atomicity]=0 )
guest_port=""

find_guest_port() {
  local args
  args="$(ps -eo args= | rg -F 'qemu-system-x86_64' | rg -F "${base_image}" | head -1 || true)"
  sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${args}"
}

while (( $(date +%s) < end )); do
  sleep 10
  guest_port="$(find_guest_port)"
  {
    echo "--- $(date '+%F %T') ---"
    if [[ -z "${guest_port}" ]]; then
      echo "guest_ssh_port=unavailable"
      continue
    fi
    snapshot="$(timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
      -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null root@127.0.0.1 \
      'test ! -e /sys/module/ext4/parameters/ext4_fault_mode && cat /sys/kernel/debug/ext4/vda/invariant_summary' \
      2>/dev/null || true)"
    printf '%s\n' "${snapshot}"
    for model in "${models[@]}"; do
      violations="$(sed -n "s/^model_${model}_violations=\\([0-9][0-9]*\\).*/\\1/p" <<<"${snapshot}" | head -1)"
      violations="${violations:-0}"
      if [[ "${violations}" =~ ^[0-9]+$ ]] && (( violations > last_model_violations[${model}] )); then
        delta=$((violations - last_model_violations[${model}]))
        echo "MODEL_CANDIDATE model=${model} violation_delta=${delta} total=${violations}"
        printf '%s\n' "${snapshot}" > "${run_dir}/candidate-${model}-${violations}.summary"
        timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
          -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
          root@127.0.0.1 'cat /sys/kernel/debug/ext4/vda/invariant_check; dmesg | tail -200' \
          > "${run_dir}/candidate-${model}-${violations}.evidence" 2>&1 || true
        cp "${manager_log}" "${run_dir}/candidate-${model}-${violations}.manager.log" || true
        break 2
      fi
      last_model_violations[${model}]="${violations}"
    done
  } >>"${monitor_log}" 2>&1
done

if [[ -z "${guest_port}" ]]; then
  echo "no guest became available; inspect ${manager_log}" >&2
  exit 1
fi
if rg -q '^MODEL_CANDIDATE ' "${monitor_log}"; then
  echo "candidate found: ${run_dir}"
  exit 10
fi
echo "no natural checker candidate in ${duration_s}s: ${run_dir}"
