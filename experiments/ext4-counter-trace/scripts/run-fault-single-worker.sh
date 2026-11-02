#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
run=${EXT4_FAULT_RUN_DIR:-$(mktemp -d "$project/results/fault-single-XXXXXXXX")}
port=${EXT4_FAULT_PORT:-11911}
key="$root/work/debian.id_rsa"
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
mkdir -p "$run"
mkdir -p "$run/evidence"
test -s "$kernel"
printf 'FAULT SINGLE START run_dir=%s port=%s workers=1\n' "$run" "$port"

cat > "$run/evidence/fault-plan.json" <<'JSON'
{
  "schema": 1,
  "fault_id": "mballoc-mark-eio",
  "operation": "mballoc_mark",
  "errno": -5,
  "count": 1,
  "program": "fault-single",
  "call_index": 0,
  "syscall_nr": 285,
  "rollback_fields": ["free_clusters", "free_inodes", "dirs", "target_file"],
  "expected_return": {"result": -1, "errno": 5}
}
JSON

gcc -O2 -Wall -Wextra -Werror -static -o "$run/fault-mballoc-single" \
    "$project/tests/fault-mballoc-single.c"
qemu-img create -q -f qcow2 -F raw -b "$root/work/debian-clean-smalljournal.img" "$run/root.qcow2"
qemu-img create -q -f raw "$run/test.raw" 200M
mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 16384 \
    -E lazy_itable_init=0 "$run/test.raw" 40000
e2fsck -fn "$run/test.raw" > "$run/baseline-fsck.txt" 2>&1 || true
printf 'FAULT SINGLE PREPARED image=%s\n' "$run/test.raw"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 1 \
    -kernel "$kernel" -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
    -drive "file=$run/test.raw,format=raw,if=virtio" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=n0 \
    -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
    -display none -monitor none -serial "file:$run/serial.log" \
    -daemonize -pidfile "$run/qemu.pid"
pid=$(<"$run/qemu.pid")
cleanup() { kill "$pid" 2>/dev/null || true; }
trap cleanup EXIT
ssh_opts=(-i "$key" -p "$port" -o BatchMode=yes -o ConnectTimeout=4 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
scp_opts=(-i "$key" -P "$port" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

ready=0
for _ in $(seq 1 180); do
    if ssh "${ssh_opts[@]}" root@127.0.0.1 'test -r /etc/os-release' \
        >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[[ "$ready" == 1 ]] || { tail -80 "$run/serial.log"; exit 1; }
printf 'FAULT SINGLE VM READY pid=%s\n' "$pid"
scp -q "${scp_opts[@]}" "$run/fault-mballoc-single" \
    "$project/tests/guest-fault-single-worker.sh" root@127.0.0.1:/tmp/
set +e
printf 'FAULT SINGLE WORKLOAD START\n'
ssh "${ssh_opts[@]}" root@127.0.0.1 'sh /tmp/guest-fault-single-worker.sh' \
    > "$run/guest.txt" 2>&1
guest_rc=$?
set -e
printf 'FAULT SINGLE WORKLOAD END guest_rc=%s\n' "$guest_rc"
scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/fault-single "$run/" || true
ssh "${ssh_opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
    echo 'guest did not power off cleanly; offline checks skipped' > "$run/fsck.txt"
    fsck_rc=99
else
    set +e
    e2fsck -fn "$run/test.raw" > "$run/fsck.txt" 2>&1
    fsck_rc=$?
    set -e
fi
python3 "$project/scripts/check-disk.py" "$run/test.raw" --output "$run/disk.json" \
    > /dev/null 2> "$run/disk-error.txt" || true
printf 'FAULT SINGLE OFFLINE CHECK fsck_rc=%s\n' "$fsck_rc"

python3 - "$run" "$guest_rc" "$fsck_rc" <<'PY'
import json
import pathlib
import re
import shutil
import sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).resolve().parent.parent / 'scripts'))
from fault_evidence import audit_trace_file

run = pathlib.Path(sys.argv[1])
guest_rc, fsck_rc = map(int, sys.argv[2:4])
raw = run / 'fault-single'
evidence = run / 'evidence'

def load(name, default=None, directory=raw):
    try:
        return json.loads((directory / name).read_text())
    except (OSError, ValueError):
        return default

result = load('workload-result.json', {}) or {}
snapshots = {name: load(name, {}) or {}
             for name in ('before.json', 'after.json', 'remount.json')}
try:
    trace = (raw / 'trace.txt').read_text(errors='replace')
except OSError:
    trace = ''

def fields(line):
    return {key: value for key, value in
            re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)', line)}

def integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

fault_hits = []
for line in trace.splitlines():
    if 'ext4_fault_inject:' not in line or 'operation=mballoc_mark' not in line:
        continue
    item = fields(line.split('ext4_fault_inject:', 1)[1])
    fault_hits.append({
        'operation': item.get('operation'),
        'errno': integer(item.get('errno', item.get('error'))),
        'program': 'fault-single',
        'call_index': integer(item.get('call_index')),
        'generation': integer(item.get('generation')),
        'injection_count': integer(item.get('injection', item.get('injection_count'))),
        'pid': integer(item.get('pid')),
        'target_pid': integer(item.get('target_pid')),
        'group': integer(item.get('group')),
        'block': integer(item.get('block')),
        'len': integer(item.get('len')),
        'result': integer(item.get('result')),
    })

status_before = {}
status_after = {}
try:
    status_before = json.loads(result.get('status_before', '{}'))
except (TypeError, ValueError):
    pass
try:
    status_after = json.loads(result.get('status_after', '{}'))
except (TypeError, ValueError):
    pass

plan = load('fault-plan.json', {}, directory=evidence) or {}
workload_pid = integer(result.get('pid'))
arm = {
    'status': 'armed' if result.get('arm_verified') is True else 'failed',
    'enabled': status_before.get('enabled'),
    'operation': 'mballoc_mark',
    'errno': -5,
    'errno_choices': status_before.get('errno_choices'),
    'errno_choice_index': integer(status_before.get('errno_choice_index')),
    'count': 1,
    'program': 'fault-single',
    'call_index': 0,
    'generation': status_before.get('generation'),
    'target_pid': status_before.get('target_pid', result.get('pid')),
    'remaining': status_before.get('remaining'),
    'injected': status_before.get('injected'),
}
(evidence / 'arm.json').write_text(json.dumps(arm, indent=2) + '\n')
trace_audit = audit_trace_file(raw / 'trace.txt', plan, arm, workload_pid)
fault_hits = trace_audit.get('hits', [])
(evidence / 'hits.json').write_text(json.dumps(fault_hits, indent=2) + '\n')
return_record = {
    'program': 'fault-single',
    'call_index': 0,
    'result': result.get('syscall_rc'),
    'errno': result.get('syscall_errno'),
    'verified': result.get('call_performed') is True and
                result.get('return_matches_expected') is True and
                trace_audit.get('marker_verified') is True,
    'native_verified': trace_audit.get('native_verified') is True,
    'marker_verified': trace_audit.get('marker_verified') is True,
    'workload_pid': workload_pid,
    'marker_id': trace_audit.get('marker_id'),
    'marker_begin_line': trace_audit.get('marker_begin_line'),
    'native_enter_line': trace_audit.get('native_enter_line'),
    'kernel_hit_line': trace_audit.get('kernel_hit_line'),
    'native_exit_line': trace_audit.get('native_exit_line'),
    'marker_end_line': trace_audit.get('marker_end_line'),
    'native_raw_result': trace_audit.get('native_raw_result'),
    'native_syscall_nr': trace_audit.get('native_syscall_nr'),
}
(evidence / 'return.json').write_text(json.dumps(return_record, indent=2) + '\n')

loss_values = []
for stats_path in sorted(raw.glob('cpu*-stats.txt')):
    text = stats_path.read_text(errors='replace')
    loss_values.extend(int(value) for value in re.findall(
        r'^(?:overrun|commit overrun|dropped events):\s*(\d+)$', text, re.M))
trace_loss = sum(loss_values) if loss_values else 1
for name, snapshot in (('stable-before.json', snapshots['before.json']),
                       ('stable-after.json', snapshots['after.json'])):
    snapshot = dict(snapshot)
    snapshot['trace_loss'] = trace_loss
    snapshot['workers'] = 1
    target_state = result.get('target_state_before' if name == 'stable-before.json'
                              else 'target_state_after')
    snapshot['target_file'] = target_state
    (evidence / name).write_text(json.dumps(snapshot, indent=2) + '\n')

try:
    disk = json.loads((run / 'disk.json').read_text())
except (OSError, ValueError):
    disk = {}

remount = dict(snapshots['remount.json'])
remount['trace_loss'] = trace_loss
remount['workers'] = 1
for name in ('workload-result.json', 'trace.txt', 'remount.json', 'cpu0-stats.txt',
             'remount-file-state.json', 'workload.txt'):
    source = raw / name
    if source.is_file():
        shutil.copy2(source, evidence / name)
(evidence / 'remount.json').write_text(json.dumps(remount, indent=2) + '\n')
(evidence / 'fsck.txt').write_text((run / 'fsck.txt').read_text(errors='replace')
                                   if (run / 'fsck.txt').exists() else '')
(evidence / 'disk.json').write_text(json.dumps(disk, indent=2) + '\n')

def same_counters(snapshot, report):
    return all(snapshot.get(field) == report.get('descriptor_totals', {}).get(field)
               for field in ('free_clusters', 'free_inodes', 'dirs'))

remount_file_state = load('remount-file-state.json', {}) or {}
remount_target = remount_file_state.get('state')
stable_target = result.get('target_state_after')

persistence = {
    'checked': guest_rc == 0 and (raw / 'remount.json').is_file() and
               (raw / 'remount-file-state.json').is_file() and
               (run / 'fsck.txt').is_file() and (run / 'disk.json').is_file(),
    'valid': disk.get('valid') is True and disk.get('status') == 'verified',
    'matches_stable_after': same_counters(snapshots['after.json'], disk),
    'matches_remount': same_counters(remount, disk) and remount_target == stable_target,
    'target_state_remount': remount_target,
    'fsck_exit': fsck_rc,
    'remount_snapshot': remount,
    'disk_status': disk.get('status'),
}
(evidence / 'persistence.json').write_text(json.dumps(persistence, indent=2) + '\n')
(evidence / 'run-meta.json').write_text(json.dumps({
    'fixture': 'fault-mballoc-single', 'workers': 1, 'guest_exit': guest_rc,
    'fsck_exit': fsck_rc, 'trace_loss': trace_loss,
    'status_before': status_before, 'status_after': status_after,
}, indent=2) + '\n')
PY
cp "$run/fault-single/trace.txt" "$run/evidence/trace.txt" 2>/dev/null || true
set +e
python3 "$project/scripts/validate-fault-evidence.py" "$run/evidence" \
    --output "$run/evidence/validation.json"
validation_rc=$?
set -e
cp "$run/evidence/validation.json" "$run/fault-evidence.json"
cp "$run/baseline-fsck.txt" "$run/evidence/baseline-fsck.txt"
printf 'fault_single_worker=complete\nrun_dir=%s\nvalidation=%s\n' \
    "$run" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["classification"])' "$run/evidence/validation.json")"
exit "$validation_rc"
