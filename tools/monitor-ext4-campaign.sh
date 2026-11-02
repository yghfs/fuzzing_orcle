#!/usr/bin/env bash
set -euo pipefail

start_epoch="$1"
manager_pid="$2"
http_url="$3"
ssh_port="$4"
ssh_key="$5"
run_dir="$6"
end_epoch="${7:-$((start_epoch + 7200))}"
qemu_match="${8:-debian-clean.img}"
log_file="${run_dir}/campaign-monitor.log"
ssh_opts=(-i "${ssh_key}" -p "${ssh_port}" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

sample_http() {
	python3 - "$http_url" <<'PY'
import sys
import urllib.request
base = sys.argv[1]
try:
    metrics = urllib.request.urlopen(base + "/metrics", timeout=15).read().decode()
    wanted = ("syz_exec_total ", "syz_corpus_cover ", "syz_crash_total ")
    print(" ".join(line for line in metrics.splitlines() if line.startswith(wanted)))
    lines = urllib.request.urlopen(base + "/log", timeout=15).read().decode().splitlines()
    for line in lines[-3:]:
        if "exec total=" in line or "crash" in line.lower() or "BUG" in line or "WARNING" in line:
            print("manager:", line)
except Exception as err:
    print("manager_http_error:", err)
PY
}

sample_guest_log() {
	local qemu_args current_port
	qemu_args="$(ps -eo args= | rg -F 'qemu-system-x86_64' | rg -F "${qemu_match}" | head -n 1 || true)"
	current_port="$(sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${qemu_args}")"
	if [[ -z "${current_port}" ]]; then
		echo "guest_ssh_port=unavailable"
		return
	fi
	timeout 25 ssh -i "${ssh_key}" -p "${current_port}" \
		-o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null root@127.0.0.1 \
		'dmesg | egrep -i "KASAN:|BUG:|WARNING:|Oops|EXT4-fs.*error|general protection|lockup|hung task" | tail -20 || true' \
		2>&1 || true
}

sample_checker_snapshot() {
	local qemu_args current_port
	local active_reproducer
	active_reproducer="$(python3 - "$http_url" <<'PY'
import sys
import urllib.request
try:
    lines = urllib.request.urlopen(sys.argv[1] + "/log", timeout=15).read().decode().splitlines()
    print("reproducing=1" if any("reproducing=1" in line for line in lines[-5:]) else "reproducing=0")
except Exception:
    print("unknown")
PY
	)"
	if [[ "${active_reproducer}" != "reproducing=0" ]]; then
		echo "checker_snapshot_skipped active_reproducer=${active_reproducer}"
		return
	fi
	qemu_args="$(ps -eo args= | rg -F 'qemu-system-x86_64' | rg -F "${qemu_match}" | head -n 1 || true)"
	current_port="$(sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${qemu_args}")"
	if [[ -z "${current_port}" ]]; then
		echo "checker_snapshot_skipped guest_ssh_port=unavailable"
		return
	fi
	timeout 25 ssh -i "${ssh_key}" -p "${current_port}" \
		-o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null root@127.0.0.1 \
		'if [ -r /sys/kernel/debug/ext4/vda/invariant_summary ]; then cat /sys/kernel/debug/ext4/vda/invariant_summary; else cat /sys/kernel/debug/ext4/vda/invariant_check | sed -n "1,8p"; fi' \
		2>&1 || true
}

sample_checker_full() {
	local qemu_args current_port
	qemu_args="$(ps -eo args= | rg -F 'qemu-system-x86_64' | rg -F "${qemu_match}" | head -n 1 || true)"
	current_port="$(sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${qemu_args}")"
	if [[ -z "${current_port}" ]]; then
		echo "checker_full_unavailable guest_ssh_port=unavailable"
		return
	fi
	timeout 30 ssh -i "${ssh_key}" -p "${current_port}" \
		-o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null root@127.0.0.1 \
		'cat /sys/kernel/debug/ext4/vda/invariant_check' 2>&1 || true
}

pause_manager() {
	python3 - "$http_url" <<'PY'
import sys
import urllib.parse
import urllib.request
url = sys.argv[1] + "/action"
body = urllib.parse.urlencode({"toggle": "pause", "url": "/stats"}).encode()
request = urllib.request.Request(url, data=body, method="POST")
try:
    urllib.request.urlopen(request, timeout=15).close()
    print("manager_pause_requested")
except Exception as err:
    print("manager_pause_error:", err)
PY
}

echo "campaign_start_epoch=${start_epoch} campaign_end_epoch=${end_epoch}" >>"${log_file}"
last_guest=0
last_signal=0
while (( $(date +%s) < end_epoch )); do
	now="$(date +%s)"
	if ! kill -0 "${manager_pid}" 2>/dev/null; then
		echo "$(date '+%F %T') manager exited before scheduled end" >>"${log_file}"
		break
	fi
	{
		echo "--- $(date '+%F %T') elapsed=$((now - start_epoch))s ---"
		sample_http
		echo "checker summary"
		checker_snapshot="$(sample_checker_snapshot)"
		printf '%s\n' "${checker_snapshot}"
		violations="$(sed -n 's/^violations_total=\([0-9][0-9]*\).*/\1/p' <<<"${checker_snapshot}" | head -1)"
		if [[ "${violations:-0}" =~ ^[0-9]+$ ]] && (( violations > last_signal )); then
			last_signal="${violations}"
			echo "checker_signal violations=${violations}; pausing for evidence"
			pause_manager
			sleep 10
			sample_checker_full > "${run_dir}/checker-signal-${now}.txt"
			sample_guest_log > "${run_dir}/dmesg-signal-${now}.txt"
			for quiet_n in 1 2 3; do
				echo "quiet_sample=${quiet_n}"
				sample_checker_snapshot
				sleep 3
			done
			pause_manager
		fi
	} >>"${log_file}" 2>&1
	if (( now - last_guest >= 300 )); then
		{
			echo "guest $(date '+%F %T')"
			sample_guest_log
		} >>"${log_file}" 2>&1
		last_guest="${now}"
	fi
	sleep 60
done

{
	echo "=== FINAL $(date '+%F %T') ==="
	sample_http
	pause_manager
} >>"${log_file}" 2>&1
sleep 10
{
	echo "guest final kernel log"
	sample_guest_log
	echo "checker final snapshot (manager paused)"
	sample_checker_snapshot
} >>"${log_file}" 2>&1
sample_checker_full > "${run_dir}/checker-full-final.txt"

if kill -0 "${manager_pid}" 2>/dev/null; then
	kill -TERM "${manager_pid}" 2>/dev/null || true
fi
echo "manager_stop_sent=$(date '+%F %T')" >>"${log_file}"
