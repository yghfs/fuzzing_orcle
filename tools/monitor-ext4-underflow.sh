#!/usr/bin/env bash
set -euo pipefail

manager_pid="$1"
end_epoch="$2"
http_url="$3"
run_dir="$4"
ssh_key="$5"
log_file="${run_dir}/underflow-monitor.log"

guest_port() {
	local args
	args="$(ps -eo args= | rg 'qemu-system-x86_64.*debian-clean.img' | head -n 1 || true)"
	sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${args}"
}

manager_stats() {
	python3 - "${http_url}" <<'PY'
import sys
import urllib.request
try:
    base = sys.argv[1]
    data = urllib.request.urlopen(base + '/metrics', timeout=8).read().decode()
    print(' '.join(line for line in data.splitlines()
                   if line.startswith(('syz_exec_total ', 'syz_corpus_cover ', 'syz_crash_total '))))
except Exception as err:
    print('manager_http_error:', err)
PY
}

pause_manager() {
	python3 - "${http_url}" <<'PY'
import sys
import urllib.parse
import urllib.request
try:
    data = urllib.parse.urlencode({'toggle': 'pause', 'url': '/stats'}).encode()
    urllib.request.urlopen(urllib.request.Request(sys.argv[1] + '/action', data=data), timeout=8).close()
    print('manager_pause_requested')
except Exception as err:
    print('manager_pause_error:', err)
PY
}

ssh_guest() {
	local port="$1"
	shift
	timeout 75 ssh -i "${ssh_key}" -p "${port}" \
		-o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
		root@127.0.0.1 "$@"
}

echo "start=$(date '+%F %T') end_epoch=${end_epoch} manager_pid=${manager_pid}" >>"${log_file}"
last_stats=0
found=0
while (( $(date +%s) < end_epoch )); do
	now="$(date +%s)"
	if ! kill -0 "${manager_pid}" 2>/dev/null; then
		echo "$(date '+%F %T') manager_exited" >>"${log_file}"
		break
	fi
	port="$(guest_port)"
	if (( now - last_stats >= 60 )); then
		{
			echo "--- $(date '+%F %T') port=${port:-unavailable} ---"
			manager_stats
		} >>"${log_file}"
		last_stats="${now}"
	fi
	if [[ -n "${port}" ]]; then
		# The first read must retain the frozen 32-edge transaction context:
		# the VM may exit before a second SSH connection can collect the ring.
		snapshot="$(ssh_guest "${port}" 'head -n 42 /sys/kernel/debug/ext4/vda/invariant_check' 2>&1)" || snapshot="guest_snapshot_error: ${snapshot}"
		if rg -q '^tx_underflows=[1-9]|^violations_total=[1-9]' <<<"${snapshot}"; then
			{
				echo "=== VIOLATION $(date '+%F %T') port=${port} ==="
				printf '%s\n' "${snapshot}"
			} >>"${log_file}"
			# Capture evidence while the VM is still alive. Pausing the manager
			# first can tear down the only QEMU before SSH completes.
			ssh_guest "${port}" 'cat /sys/kernel/debug/ext4/vda/invariant_check' \
				>"${run_dir}/underflow-events.txt" 2>"${run_dir}/underflow-events.err" || true
			ssh_guest "${port}" 'dmesg' \
				>"${run_dir}/underflow-dmesg.txt" 2>"${run_dir}/underflow-dmesg.err" || true
			pause_manager >>"${log_file}" 2>&1
			found=1
			break
		fi
		if [[ "${snapshot}" == guest_snapshot_error:* ]]; then
			printf '%s\n' "${snapshot}" >>"${log_file}"
		fi
	fi
	sleep 20
done

{
	echo "=== END $(date '+%F %T') found=${found} ==="
	manager_stats
	if (( !found )); then
		port="$(guest_port)"
		if [[ -n "${port}" ]]; then
			ssh_guest "${port}" 'head -n 8 /sys/kernel/debug/ext4/vda/invariant_check; dmesg | grep -Ei "EXT4 invariant violation|KASAN:|BUG:|Oops|EXT4-fs.*error" | tail -20 || true' || true
		fi
	fi
} >>"${log_file}" 2>&1

if kill -0 "${manager_pid}" 2>/dev/null; then
	kill -TERM "${manager_pid}" 2>/dev/null || true
fi
echo "manager_stop_sent=$(date '+%F %T')" >>"${log_file}"
