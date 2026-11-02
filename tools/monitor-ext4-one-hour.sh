#!/usr/bin/env bash
set -euo pipefail

manager_pid="$1"
end_epoch="$2"
http_url="$3"
run_dir="$4"
ssh_key="$5"
log_file="${run_dir}/campaign-monitor.log"
start_epoch="$(date +%s)"

guest_port() {
	local qemu_args
	qemu_args="$(ps -eo args= | rg 'qemu-system-x86_64.*debian-clean.img' | head -n 1 || true)"
	sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${qemu_args}"
}

metrics() {
	python3 - "${http_url}" <<'PY'
import sys
import urllib.request
try:
    base = sys.argv[1]
    data = urllib.request.urlopen(base + '/metrics', timeout=12).read().decode()
    print(' '.join(line for line in data.splitlines()
                   if line.startswith(('syz_exec_total ', 'syz_corpus_cover ', 'syz_crash_total '))))
    log = urllib.request.urlopen(base + '/log', timeout=12).read().decode().splitlines()
    print('manager:', next((line for line in reversed(log) if 'exec total=' in line), 'no stats yet'))
except Exception as err:
    print('manager_http_error:', err)
PY
}

guest() {
	local port
	port="$(guest_port)"
	if [[ -z "${port}" ]]; then
		echo 'guest_ssh_port=unavailable'
		return
	fi
	echo "guest_ssh_port=${port}"
	timeout 55 ssh -i "${ssh_key}" -p "${port}" \
		-o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@127.0.0.1 \
		'head -n 8 /sys/kernel/debug/ext4/vda/invariant_check; dmesg | egrep -i "EXT4 invariant violation|KASAN:|BUG:|Oops|EXT4-fs.*error" | tail -16 || true; dmesg | egrep -i "WARNING:|general protection|lockup|hung task" | tail -8 || true' \
		2>&1 || true
}

echo "campaign_start_epoch=${start_epoch} campaign_end_epoch=${end_epoch}" >>"${log_file}"
last_guest=0
while (( $(date +%s) < end_epoch )); do
	now="$(date +%s)"
	if ! kill -0 "${manager_pid}" 2>/dev/null; then
		echo "$(date '+%F %T') manager exited early" >>"${log_file}"
		break
	fi
	{
		echo "--- $(date '+%F %T') elapsed=$((now - start_epoch))s ---"
		metrics
		if (( now - last_guest >= 300 || end_epoch - now < 90 )); then
			guest
			last_guest="${now}"
		fi
	} >>"${log_file}" 2>&1
	now="$(date +%s)"
	if (( now < end_epoch )); then
		sleep "$(( end_epoch - now < 60 ? end_epoch - now : 60 ))"
	fi
done

{
	echo "=== FINAL $(date '+%F %T') ==="
	metrics
	# Pause generation before the final snapshot, so there are no live writers.
	python3 - "${http_url}" <<'PY'
import sys
import urllib.parse
import urllib.request
try:
    data = urllib.parse.urlencode({'toggle': 'pause', 'url': '/stats'}).encode()
    urllib.request.urlopen(urllib.request.Request(sys.argv[1] + '/action', data=data), timeout=12).close()
    print('manager_pause_requested')
except Exception as err:
    print('manager_pause_error:', err)
PY
	sleep 8
	guest
} >>"${log_file}" 2>&1

if kill -0 "${manager_pid}" 2>/dev/null; then
	kill -TERM "${manager_pid}" 2>/dev/null || true
fi
echo "manager_stop_sent=$(date '+%F %T')" >>"${log_file}"
