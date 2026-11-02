#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${root_dir}/kernel/linux-6.12.1/arch/x86/boot/bzImage"
base_image="${EXT4_BASE_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
ssh_key="${work_dir}/debian.id_rsa"
base_port="${1:-10190}"
cycles="${EXT4_PAIR_CYCLES:-5000}"
duration="${EXT4_PAIR_DURATION:-120}"
modes="${EXT4_PAIR_MODES:-punch truncate checkpoint pf cf pt pc tc pcf tcf ptf ptc pff ptcf}"
stamp="$(date +%Y%m%d%H%M%S)-$$"
campaign_dir="${work_dir}/ext4-journal-pair-targeted-${stamp}"

mkdir -p "${campaign_dir}"

run_mode() {
	local mode="$1"
	local port="$2"
	local run_dir="${campaign_dir}/${mode}"
	local overlay="${run_dir}/disk.qcow2"
	local pid_file="${run_dir}/qemu.pid"
	local serial_log="${run_dir}/serial.log"
	local remote_log="/tmp/ext4-pair-${mode}.log"
	local ssh_opts
	local remote_rc=0

	mkdir -p "${run_dir}"
	qemu-img create -f qcow2 -F raw -b "${base_image}" "${overlay}" >/dev/null
	qemu-system-x86_64 \
		-machine q35,accel=tcg -cpu max -m 1024 -smp 2 \
		-kernel "${kernel_image}" \
		-drive "file=${overlay},format=qcow2,if=virtio" \
		-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
		-device virtio-net-pci,netdev=n0 \
		-append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=65536" \
		-display none -monitor none -serial "file:${serial_log}" -daemonize \
		-pidfile "${pid_file}"

	ssh_opts=(-i "${ssh_key}" -p "${port}" -o BatchMode=yes -o ConnectTimeout=3 \
		-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
	for _ in $(seq 1 120); do
		if ssh "${ssh_opts[@]}" root@127.0.0.1 \
			'test -r /sys/kernel/debug/ext4/vda/invariant_check' >/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'test -r /sys/kernel/debug/ext4/vda/invariant_check'

	scp -q -i "${ssh_key}" -P "${port}" -o StrictHostKeyChecking=no \
		"${work_dir}/ext4-journal-pair-targeted-static" \
		root@127.0.0.1:/tmp/ext4-journal-pair-targeted-static
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'printf 1 > /sys/kernel/debug/ext4/vda/invariant_reset'
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		"cat /sys/kernel/debug/ext4/vda/invariant_check | sed -n '1,10p'" \
		> "${run_dir}/before.txt"

	ssh "${ssh_opts[@]}" root@127.0.0.1 "
set -eu
chmod +x /tmp/ext4-journal-pair-targeted-static
timeout --foreground ${duration}s /tmp/ext4-journal-pair-targeted-static \
  /tmp/ext4-pair-${mode} ${mode} ${cycles} > ${remote_log} 2>&1 || rc=\$?
rc=\${rc:-0}
cat ${remote_log}
printf 'workload_exit=%s mode=%s cycles=%s\\n' \"\$rc\" ${mode} ${cycles}
" > "${run_dir}/program.txt" 2>&1 || remote_rc=$?

	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		'cat /sys/kernel/debug/ext4/vda/invariant_check' \
		> "${run_dir}/report.txt" 2>&1 || true
	ssh "${ssh_opts[@]}" root@127.0.0.1 dmesg \
		> "${run_dir}/dmesg.txt" 2>&1 || true
	ssh "${ssh_opts[@]}" root@127.0.0.1 \
		"sync; poweroff -f" >/dev/null 2>&1 || true
	sleep 2
	if [[ -s "${pid_file}" ]]; then
		kill "$(<"${pid_file}")" 2>/dev/null || true
	fi

	{
		echo "mode=${mode} port=${port} remote_rc=${remote_rc}"
		rg -m 1 '^(snapshot|violations_total|last_violation|unverified_total|snapshot_races|snapshots|tx_underflows|events|tx_context)' \
			"${run_dir}/report.txt" || true
		echo -n "pairing_dmesg="
		grep -c 'transaction pairing violation' "${run_dir}/dmesg.txt" || true
	} > "${run_dir}/summary.txt"
}

port_offset=0
for mode in ${modes}; do
	run_mode "${mode}" "$((base_port + port_offset))"
	port_offset=$((port_offset + 1))
done

echo "campaign_dir=${campaign_dir}"
