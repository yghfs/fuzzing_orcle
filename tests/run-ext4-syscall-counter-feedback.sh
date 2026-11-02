#!/usr/bin/env bash
set -euo pipefail

# Syzkaller keeps its full syscall mutation loop. This controller adds targeted
# seeds and scores them using checker counter deltas, an independent feedback
# channel for the runtime invariant models.
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${root_dir}/work"
kernel_image="${COUNTER_FEEDBACK_KERNEL:-${work_dir}/kernels/ext4-count-normal/bzImage}"
base_image="${COUNTER_FEEDBACK_IMAGE:-${work_dir}/debian-clean-smalljournal.img}"
config_file="${COUNTER_FEEDBACK_CONFIG:-${root_dir}/tools/ext4-full-manager.cfg}"
seed_dir="${COUNTER_FEEDBACK_SEED_DIR:-${root_dir}/tests/ext4-counter-feedback}"
run_dir="${COUNTER_FEEDBACK_RUN_DIR:-${work_dir}/ext4-counter-feedback-$(date +%Y%m%d%H%M%S)-$$}"
duration_s="${COUNTER_FEEDBACK_DURATION:-3600}"
interval_s="${COUNTER_FEEDBACK_INTERVAL:-30}"
warmup_attempts="${COUNTER_FEEDBACK_WARMUP_ATTEMPTS:-2}"
http_port="${COUNTER_FEEDBACK_HTTP_PORT:-56846}"
event_cap="${COUNTER_FEEDBACK_EVENT_CAP:-65536}"
checker_device="${COUNTER_FEEDBACK_CHECKER_DEVICE:-vda}"
pressure_mount="${COUNTER_FEEDBACK_NATURAL_PRESSURE_MOUNT:-/mnt/ext4-pressure}"

mkdir -p "${run_dir}"
pressure_bin="${run_dir}/ext4-natural-pressure-workload"
gcc -O2 -Wall -Wextra -Werror -static -o "${pressure_bin}" \
	"${root_dir}/tests/ext4-natural-pressure-workload.c"
manager_bin="${root_dir}/tools/syzkaller/bin/syz-manager"
ssh_key="${work_dir}/debian.id_rsa"
test -x "${manager_bin}" || { echo "missing syz-manager" >&2; exit 2; }
test -r "${kernel_image}" || { echo "missing kernel: ${kernel_image}" >&2; exit 2; }
test -r "${base_image}" || { echo "missing image: ${base_image}" >&2; exit 2; }
test -r "${config_file}" || { echo "missing config: ${config_file}" >&2; exit 2; }
test -r "${ssh_key}" || { echo "missing ssh key: ${ssh_key}" >&2; exit 2; }
for arm in inode_bitmap; do
	test -r "${seed_dir}/${arm}.syz" || { echo "missing feedback seed: ${seed_dir}/${arm}.syz" >&2; exit 2; }
done
if grep -Eq '^CONFIG_EXT4_FAULT_INJECTION=y|^CONFIG_EXT4_FAULT_INJECTION=m' "${root_dir}/kernel/linux-6.12.1/.config"; then
	echo "source config has fault injection enabled; refusing discovery run" >&2
	exit 2
fi

base_image="$(readlink -f "${base_image}")"
kernel_image="$(readlink -f "${kernel_image}")"
port_cfg="${run_dir}/manager.json"
pressure_image=""
if [[ "${COUNTER_FEEDBACK_NATURAL_PRESSURE_DISK:-0}" == 1 ]]; then
	pressure_image="${run_dir}/natural-pressure.raw"
	checker_device="${COUNTER_FEEDBACK_CHECKER_DEVICE:-vdb}"
	truncate -s "${COUNTER_FEEDBACK_NATURAL_PRESSURE_SIZE:-2G}" "${pressure_image}"
	if [[ "${COUNTER_FEEDBACK_NATURAL_QUOTA:-0}" == 1 ]]; then
		mkfs.ext4 -F -q -O quota "${pressure_image}"
	else
		mkfs.ext4 -F -q "${pressure_image}"
	fi
	python3 - "${config_file}" "${port_cfg}" "${run_dir}" "${base_image}" "${kernel_image}" "${http_port}" "${event_cap}" "${pressure_image}" "${root_dir}" <<'PY'
import json, os, sys
source, target, run_dir, image, kernel, port, event_cap, pressure_disk, root = sys.argv[1:]
cfg = json.load(open(source))
cfg["http"] = f"127.0.0.1:{port}"
cfg["workdir"] = f"{run_dir}/syz-work"
cfg["kernel_obj"] = f"{root}/kernel/linux-6.12.1"
cfg["image"] = image
cfg["procs"] = int(os.environ.get("COUNTER_FEEDBACK_PROCS", cfg.get("procs", 1)))
cfg["vm"]["kernel"] = kernel
cfg["vm"]["cmdline"] = cfg["vm"].get("cmdline", "") + f" ext4.ext4_checker_event_cap={event_cap}"
cfg["vm"]["qemu_args"] = cfg["vm"].get("qemu_args", "") + f" -drive file={pressure_disk},format=raw,if=none,id=pressure -device virtio-blk-pci,drive=pressure,addr=0x10"
json.dump(cfg, open(target, "w"), indent=2)
PY
else
sed -e "s#\"http\": \"127.0.0.1:[0-9]*\"#\"http\": \"127.0.0.1:${http_port}\"#" \
	-e "s#\"workdir\": \"[^\"]*\"#\"workdir\": \"${run_dir}/syz-work\"#" \
	-e "s#\"kernel_obj\": \"[^\"]*\"#\"kernel_obj\": \"${root_dir}/kernel/linux-6.12.1\"#" \
	-e "s#\"image\": \"[^\"]*\"#\"image\": \"${base_image}\"#" \
	-e "s#\"kernel\": \"[^\"]*\"#\"kernel\": \"${kernel_image}\"#" \
	-e "s#\"procs\": [0-9]*#\"procs\": ${COUNTER_FEEDBACK_PROCS:-4}#" \
	-e "s#ext4_checker_event_cap=[0-9]*#ext4_checker_event_cap=${event_cap}#" \
	"${config_file}" > "${port_cfg}"
fi

manager_log="${run_dir}/manager.log"
feedback_log="${run_dir}/feedback.log"
manager_pid=""
cleanup() { [[ -z "${manager_pid}" ]] || kill "${manager_pid}" 2>/dev/null || true; }
trap cleanup EXIT
"${manager_bin}" -config="${port_cfg}" >"${manager_log}" 2>&1 &
manager_pid=$!

find_guest_port() {
	local args
	args="$(ps -eo args= | rg -F 'qemu-system-x86_64' | rg -F "${base_image}" | head -1 || true)"
	sed -n 's/.*hostfwd=tcp:127\.0\.0\.1:\([0-9][0-9]*\)-:22.*/\1/p' <<<"${args}"
}
ssh_snapshot() {
	local guest_port="$1"
	timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
		-o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null root@127.0.0.1 \
		"test ! -e /sys/module/ext4/parameters/ext4_fault_mode && cat /sys/kernel/debug/ext4/${checker_device}/invariant_summary" \
		2>/dev/null || true
}
field() {
	sed -n "s/^$1=\([-0-9][0-9]*\).*/\1/p" <<<"$2" | head -1
}
save_candidate() {
	local guest_port="$1" snapshot="$2" arm="$3" round="$4" kind="${5:-model}"
	local prefix="${kind}-${arm}-${round}"
	printf '%s\n' "${snapshot}" > "${run_dir}/${prefix}.summary"
	timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
		-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		root@127.0.0.1 "cat /sys/kernel/debug/ext4/${checker_device}/invariant_check; dmesg | tail -300" \
		> "${run_dir}/${prefix}.evidence" 2>&1 || true
	if [[ -r "${seed_dir}/${arm}.syz" ]]; then
		cp "${seed_dir}/${arm}.syz" "${run_dir}/${prefix}.${arm}.syz"
	fi
	cp "${manager_log}" "${run_dir}/${prefix}.manager.log" || true
}

guest_port=""
for _ in $(seq 1 120); do
	guest_port="$(find_guest_port)"
	if [[ -n "${guest_port}" ]] && timeout 10 ssh -i "${ssh_key}" -p "${guest_port}" \
		-o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null root@127.0.0.1 \
		'test -r /sys/kernel/debug/ext4/vda/invariant_summary' \
		>/dev/null 2>&1; then break; fi
	sleep 1
done
[[ -n "${guest_port}" ]] || { echo "no guest became available; inspect ${manager_log}" >&2; exit 1; }
if [[ "${COUNTER_FEEDBACK_NATURAL_PRESSURE:-1}" == 1 ]]; then
	quota_mode="${COUNTER_FEEDBACK_NATURAL_QUOTA:-0}"
	if [[ "${COUNTER_FEEDBACK_NATURAL_QUOTA_ONLY:-0}" == 1 ]]; then
		quota_mode=2
	fi
	if [[ -n "${pressure_image}" ]]; then
		mount_opts=""
		if [[ "${COUNTER_FEEDBACK_NATURAL_QUOTA:-0}" == 1 ]]; then
			mount_opts="-o usrquota"
		fi
		timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
			-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
			root@127.0.0.1 "mkdir -p '${pressure_mount}' && mount ${mount_opts} /dev/${checker_device} '${pressure_mount}'"
	fi
	scp -q -i "${ssh_key}" -P "${guest_port}" -o BatchMode=yes \
		-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		"${pressure_bin}" root@127.0.0.1:/tmp/ext4-natural-pressure-workload
	workload_dir="/tmp"
	[[ -z "${pressure_image}" ]] || workload_dir="${pressure_mount}"
	timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
		-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		root@127.0.0.1 "nohup /tmp/ext4-natural-pressure-workload '${workload_dir}' ${quota_mode} /dev/${checker_device} \
		>/tmp/ext4-natural-pressure-workload.log 2>&1 </dev/null &"
	echo "natural pressure workload started" | tee -a "${feedback_log}"
fi
http_get() {
	local url="$1"
	if command -v curl >/dev/null 2>&1; then
		curl -fsS "$url" >/dev/null
	else
		wget -q -O /dev/null "$url"
	fi
}
post_candidate_python() {
	local seed="$1"
	python3 - "${seed}" "http://127.0.0.1:${http_port}/addcandidate" <<'PY'
import sys, uuid, urllib.request
seed, url = sys.argv[1:]
boundary = uuid.uuid4().hex.encode()
data = open(seed, 'rb').read()
body = (b'--' + boundary + b'\r\n'
        b'Content-Disposition: form-data; name="file"; filename="seed.syz"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n' + data +
        b'\r\n--' + boundary + b'--\r\n')
req = urllib.request.Request(url, data=body, method='POST',
    headers={'Content-Type': 'multipart/form-data; boundary=' + boundary.decode()})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(resp.status)
except urllib.error.HTTPError as exc:
    print(exc, exc.read().decode(errors='replace'), file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
PY
}
post_candidate() {
	local seed="$1" body boundary attempt
	if ! command -v curl >/dev/null 2>&1; then
		boundary="ext4counter$(date +%s%N)"
		body="${run_dir}/.candidate-${$}.multipart"
		{
			printf -- '--%s\r\n' "${boundary}"
			printf -- 'Content-Disposition: form-data; name="file"; filename="seed.syz"\r\n'
			printf -- 'Content-Type: application/octet-stream\r\n\r\n'
			cat "${seed}"
			printf -- '\r\n--%s--\r\n' "${boundary}"
		} >"${body}"
	fi
	for attempt in $(seq 1 30); do
		if command -v curl >/dev/null 2>&1; then
			curl -fsS -X POST -F "file=@${seed}" "http://127.0.0.1:${http_port}/addcandidate" >/dev/null && return 0
		else
			post_candidate_python "${seed}" && return 0
		fi
		sleep 1
	done
	return 1
}
for _ in $(seq 1 30); do
	http_get "http://127.0.0.1:${http_port}/" >/dev/null 2>&1 && break
	sleep 1
done
manager_ready=0
for _ in $(seq 1 90); do
	if http_get "http://127.0.0.1:${http_port}/corpus" >/dev/null 2>&1; then
		manager_ready=1
		break
	fi
	sleep 1
done
if (( manager_ready == 0 )); then
	echo "manager corpus is not ready; inspect ${manager_log}" >&2
	exit 1
fi

declare -a arms=(inode_bitmap)
# Completed-check targets are deliberately modest: once an arm reaches its
# target it still receives exploration rounds, while under-covered models get
# a strong deficit bonus.
declare -A check_targets=( [inode_bitmap]=128 )
declare -A attempts=( [inode_bitmap]=0 )
declare -A ema_gain=( [inode_bitmap]=0 )
total_rounds=0
start="$(date +%s)"
end=$((start + duration_s))
previous="$(ssh_snapshot "${guest_port}")"
last_violations="$(field violations_total "${previous}")"
last_violations="${last_violations:-0}"
models=(inode_bitmap block_bitmap directory_link_count mkdir_atomicity)
declare -A last_model_violations=( [inode_bitmap]=0 [block_bitmap]=0 [directory_link_count]=0 [mkdir_atomicity]=0 )
for model in "${models[@]}"; do
	value="$(field "model_${model}_violations" "${previous}")"
	last_model_violations[${model}]="${value:-0}"
done
found_candidate=0

choose_arm() {
	local arm score best_arm="" best_score="-1" checks tries gain
	for arm in "${arms[@]}"; do
		if (( attempts[${arm}] == 0 )); then echo "${arm}"; return; fi
		checks="$(field "model_${arm}_checks" "${previous}")"
		tries="${attempts[${arm}]}"
		# Keep giving a zero-coverage arm a few direct seed insertions before
		# letting the other models dominate the schedule.
		if (( tries < warmup_attempts )) && (( ${checks:-0} == 0 )); then
			echo "${arm}"
			return
		fi
		# Project-specific policy: balance completed checker coverage against a
		# per-model target, exploit recent completed-check gains after this arm's
		# seeds, and retain an exploration bonus. Unverified samples never count
		# as reward because they do not demonstrate that the invariant ran.
		score="$(awk -v gain="${ema_gain[${arm}]}" -v tries="${tries}" \
			-v total="${total_rounds}" -v checks="${checks:-0}" \
			-v target="${check_targets[${arm}]}" \
			'BEGIN { deficit = 2.0 / (1.0 + checks / target); exploit = gain / (1.0 + sqrt(tries)); explore = sqrt(2.0 * log(total + 1.0) / tries); print deficit + exploit + explore }')"
		if [[ -z "${best_arm}" ]] || awk -v a="${score}" -v b="${best_score}" 'BEGIN { exit !(a > b) }'; then
			best_arm="${arm}"; best_score="${score}"
		fi
	done
	echo "${best_arm}"
}

while (( $(date +%s) < end )); do
	arm="$(choose_arm)"
	seed="${seed_dir}/${arm}.syz"
	if post_candidate "${seed}"; then
		echo "round=${total_rounds} arm=${arm} addcandidate=ok" >> "${feedback_log}"
	else
		echo "round=${total_rounds} arm=${arm} addcandidate=failed" | tee -a "${feedback_log}" >&2
	fi
	attempts[${arm}]=$((attempts[${arm}] + 1))
	total_rounds=$((total_rounds + 1))
	sleep "${interval_s}"
	guest_port="$(find_guest_port)"
	snapshot="$(ssh_snapshot "${guest_port}")"
	[[ -n "${snapshot}" ]] || { echo "round=${total_rounds} checker=unavailable" >> "${feedback_log}"; continue; }
	violations="$(field violations_total "${snapshot}")"
	if [[ "${violations:-0}" =~ ^[0-9]+$ ]] && (( violations < last_violations )); then
		# A manager VM restart resets checker counters; establish a new baseline.
		last_violations="${violations}"
		previous="${snapshot}"
		continue
	fi
	last_violations="${violations:-${last_violations}}"
	for model in "${models[@]}"; do
		model_violations="$(field "model_${model}_violations" "${snapshot}")"
		model_violations="${model_violations:-0}"
		if [[ "${model_violations}" =~ ^[0-9]+$ ]] &&
		   (( model_violations > last_model_violations[${model}] )); then
			delta_model_violations=$((model_violations - last_model_violations[${model}]))
			echo "MODEL_CANDIDATE round=${total_rounds} arm=${arm} model=${model} violation_delta=${delta_model_violations}" | tee -a "${feedback_log}"
			save_candidate "${guest_port}" "${snapshot}" "${arm}" "${total_rounds}" "model-${model}"
			found_candidate=1
		fi
		last_model_violations[${model}]="${model_violations}"
	done
	before_checks="$(field "model_${arm}_checks" "${previous}")"
	after_checks="$(field "model_${arm}_checks" "${snapshot}")"
	before_unknown="$(field "model_${arm}_unverified" "${previous}")"
	after_unknown="$(field "model_${arm}_unverified" "${snapshot}")"
	delta_checks=$(( ${after_checks:-0} - ${before_checks:-0} ))
	delta_unknown=$(( ${after_unknown:-0} - ${before_unknown:-0} ))
	(( delta_checks < 0 )) && delta_checks=0
	(( delta_unknown < 0 )) && delta_unknown=0
	model_deltas=""
	for model in "${arms[@]}"; do
		old="$(field "model_${model}_checks" "${previous}")"
		new="$(field "model_${model}_checks" "${snapshot}")"
		delta=$(( ${new:-0} - ${old:-0} ))
		(( delta < 0 )) && delta=0
		if (( ${new:-0} >= ${old:-0} )); then
			ema_gain[${model}]="$(awk -v old="${ema_gain[${model}]}" -v gain="${delta}" 'BEGIN { print 0.6 * old + 0.4 * gain }')"
		fi
		model_deltas+=" ${model}:${delta}->${new:-0}"
	done
	error_deltas=""
	for source in inode_alloc inode_free; do
		hits="$(field "error_exit_${source}_hits" "${snapshot}")"
		verified="$(field "error_exit_${source}_verified" "${snapshot}")"
		enospc="$(field "error_exit_${source}_enospc" "${snapshot}")"
		edquot="$(field "error_exit_${source}_edquot" "${snapshot}")"
		eio="$(field "error_exit_${source}_eio" "${snapshot}")"
		enomem="$(field "error_exit_${source}_enomem" "${snapshot}")"
		other="$(field "error_exit_${source}_other" "${snapshot}")"
		last_errno="$(field "error_exit_${source}_last_errno" "${snapshot}")"
		last_expected="$(field "error_exit_${source}_last_expected" "${snapshot}")"
		last_actual="$(field "error_exit_${source}_last_actual" "${snapshot}")"
		error_deltas+=" ${source}:hits=${hits:-0},verified=${verified:-0},ENOSPC=${enospc:-0},EDQUOT=${edquot:-0},EIO=${eio:-0},ENOMEM=${enomem:-0},other=${other:-0},last_errno=${last_errno:-0},expected=${last_expected:-0},actual=${last_actual:-0}"
	done
	echo "round=${total_rounds} arm=${arm} target_checks_delta=${delta_checks} target_unverified_delta=${delta_unknown} target_ema=${ema_gain[${arm}]} model_checks{${model_deltas} } error_exits{${error_deltas} } violations=${violations:-0}" | tee -a "${feedback_log}"
	previous="${snapshot}"
done

cp "${manager_log}" "${run_dir}/manager-final.log" || true
if [[ "${COUNTER_FEEDBACK_NATURAL_PRESSURE:-1}" == 1 ]]; then
	timeout 30 ssh -i "${ssh_key}" -p "${guest_port}" \
		-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		root@127.0.0.1 'cat /tmp/ext4-natural-pressure-workload.log' \
		>"${run_dir}/natural-pressure.log" 2>&1 || true
fi
if (( found_candidate )); then
	printf 'checker candidate(s) saved; feedback rounds=%s run_dir=%s\n' \
		"${total_rounds}" "${run_dir}"
	exit 10
fi
printf 'no natural checker candidate in %ss; feedback rounds=%s run_dir=%s\n' \
	"${duration_s}" "${total_rounds}" "${run_dir}"
