#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel="${EXT4_DIRLINK_KERNEL:-${work_dir}/kernels/ext4-count-normal-new/bzImage}"
kernel_config="${EXT4_DIRLINK_KERNEL_CONFIG:-$(dirname "${kernel}")/config}"
root_base="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
test_base="${EXT4_DIRLINK_BASE:-${work_dir}/ext4-dir-link-count-natural-1g.raw}"
port="${EXT4_DIRLINK_PORT:-11692}"
workers="${EXT4_DIRLINK_WORKERS:-8}"
rounds="${EXT4_DIRLINK_ROUNDS:-20}"
iterations="${EXT4_DIRLINK_ITERATIONS:-500}"
run_dir="${EXT4_DIRLINK_RUN_DIR:-${work_dir}/ext4-dir-link-count-natural-$(date +%Y%m%d%H%M%S)-$$}"
ssh_key="${work_dir}/debian.id_rsa"
if ! grep -q '^CONFIG_EXT4_INVARIANT_CHECKER=y$' "${kernel_config}" ||
	grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=(y|m)$' "${kernel_config}"; then
	echo "kernel config must enable the checker and disable fault injection" >&2
	exit 2
fi
mkdir -p "${run_dir}"
program="${run_dir}/ext4-dir-link-count-stress"
gcc -O2 -Wall -Wextra -Werror -static -o "${program}" "${root_dir}/tests/ext4-dir-link-count-stress.c"
if [[ ! -e "${test_base}" ]]; then
	qemu-img create -q -f raw "${test_base}" 1G
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 32768 -E lazy_itable_init=0 "${test_base}"
fi
qemu-img create -q -f qcow2 -F raw -b "${root_base}" "${run_dir}/root.qcow2"
qemu-img create -q -f qcow2 -F raw -b "${test_base}" "${run_dir}/test.qcow2"
qemu_pid="${run_dir}/qemu.pid"
qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
	-kernel "${kernel}" -drive "file=${run_dir}/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=${run_dir}/test.qcow2,format=qcow2,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" -device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536' \
	-display none -monitor none -serial "file:${run_dir}/serial.log" -daemonize -pidfile "${qemu_pid}"
qemu_pid_value="$(<"${qemu_pid}")"
trap 'kill "${qemu_pid_value}" 2>/dev/null || true' EXIT
opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 120); do
	if ssh "${opts[@]}" root@127.0.0.1 'mkdir -p /mnt/ext4-dirlink; mountpoint -q /mnt/ext4-dirlink || mount /dev/vdb /mnt/ext4-dirlink; test -r /sys/kernel/debug/ext4/vdb/invariant_summary' >/dev/null 2>&1; then break; fi
	sleep 1
done
ssh "${opts[@]}" root@127.0.0.1 'test ! -e /sys/module/ext4/parameters/ext4_fault_mode'
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${program}" root@127.0.0.1:/tmp/ext4-dir-link-count-stress
ssh "${opts[@]}" root@127.0.0.1 "printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; timeout --foreground 900s /tmp/ext4-dir-link-count-stress /mnt/ext4-dirlink/tree ${workers} ${rounds} ${iterations} > /tmp/ext4-dir-link-count-stress.log 2>&1 & workload_pid=\$!; poll_snapshots=0; while kill -0 \$workload_pid 2>/dev/null; do cat /sys/kernel/debug/ext4/vdb/invariant_summary > /tmp/ext4-dir-link-checker-during.txt; poll_snapshots=\$((poll_snapshots + 1)); sleep 0.05; done; set +e; wait \$workload_pid; workload_rc=\$?; set -e; echo workload_rc=\$workload_rc poll_snapshots=\$poll_snapshots; cat /tmp/ext4-dir-link-count-stress.log; sync; test \$workload_rc -eq 0" >"${run_dir}/workload.txt"
if ! rg -q 'poll_snapshots=[1-9][0-9]*' "${run_dir}/workload.txt"; then
	echo 'checker was not polled during the syscall workload' >&2
	exit 1
fi
scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	root@127.0.0.1:/tmp/ext4-dir-link-checker-during.txt "${run_dir}/checker-during.txt"
if ! rg -q '^directory_link_count_check=PASS ' "${run_dir}/workload.txt"; then
	echo 'independent namespace link-count scan did not pass' >&2
	exit 1
fi
ssh "${opts[@]}" root@127.0.0.1 \
	'printf 1 > /sys/kernel/debug/ext4/vdb/invariant_reset; cat /sys/kernel/debug/ext4/vdb/invariant_summary; cat /sys/kernel/debug/ext4/vdb/invariant_check' \
	>"${run_dir}/checker.txt"
if ! rg -q '^snapshot OK groups=' "${run_dir}/checker.txt" ||
	! rg -q '^violations_total=0$' "${run_dir}/checker.txt" ||
	! rg -q '^snapshot_races=0$' "${run_dir}/checker.txt"; then
	echo 'stable post-workload checker snapshot did not pass' >&2
	tail -60 "${run_dir}/checker.txt" >&2
	exit 1
fi
ssh "${opts[@]}" root@127.0.0.1 'dmesg | grep -E "EXT4-fs.*error|BUG:|WARNING:" | tail -100 || true' >"${run_dir}/dmesg.txt"
ssh "${opts[@]}" root@127.0.0.1 'sync; systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 45); do kill -0 "${qemu_pid_value}" 2>/dev/null || break; sleep 1; done
if kill -0 "${qemu_pid_value}" 2>/dev/null; then
	echo 'guest did not power off cleanly' >"${run_dir}/fsck.txt"
	fsck_rc=125
else
	qemu-img convert -q -f qcow2 -O raw "${run_dir}/test.qcow2" "${run_dir}/recovered.raw"
	set +e
	e2fsck -fn "${run_dir}/recovered.raw" >"${run_dir}/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
fi
printf 'run_dir=%s fsck_rc=%s\n' "${run_dir}" "${fsck_rc}" | tee "${run_dir}/report.txt"
rg -n 'directory_link_count|directory_count_stress|workload_rc|poll_snapshots|model_|violations_total|snapshots=' "${run_dir}/workload.txt" "${run_dir}/checker.txt" "${run_dir}/checker-during.txt" "${run_dir}/dmesg.txt" | tee -a "${run_dir}/report.txt" || true
tail -20 "${run_dir}/fsck.txt"
