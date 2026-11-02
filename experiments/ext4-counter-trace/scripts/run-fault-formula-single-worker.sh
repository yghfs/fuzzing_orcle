#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$project/../.." && pwd)
fault_id=inode-evict-final-dirty
run=${EXT4_FAULT_RUN_DIR:-}
port=${EXT4_FAULT_PORT:-11912}
build_kernel=${EXT4_FAULT_BUILD_KERNEL:-0}
dry_plan=0
control=0
errno_override=''

usage() {
	echo "usage: $0 [--fault-id PLAN] [--errno ERRNO] [--run-dir DIR] [--port PORT] [--build-kernel] [--dry-plan] [--control]" >&2
}

while (($#)); do
	case "$1" in
		--fault-id)
			(($# >= 2)) || { usage; exit 2; }
			fault_id=$2
			shift 2
			;;
		--errno)
			(($# >= 2)) || { usage; exit 2; }
			errno_override=$2
			shift 2
			;;
		--run-dir)
			(($# >= 2)) || { usage; exit 2; }
			run=$2
			shift 2
			;;
		--port)
			(($# >= 2)) || { usage; exit 2; }
			port=$2
			shift 2
			;;
		--build-kernel)
			build_kernel=1
			shift
			;;
		--dry-plan)
			dry_plan=1
			shift
			;;
		--control)
			control=1
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			usage
			exit 2
			;;
	esac
done

# Resolve the selected plan before building or booting. Keep this action map in
# sync with prepare_action() in fault-formula-single.c; unsupported actions
# must fail closed rather than being reported as exercised.
plan_json=$(python3 - "$project" "$fault_id" "$errno_override" <<'PY'
import json
import pathlib
import sys

project = pathlib.Path(sys.argv[1])
fault_id = sys.argv[2]
errno_override = sys.argv[3] or None
sys.path.insert(0, str(project / "scripts"))
from fault_plan import get_plan
from fault_errno_catalog import errno_codes

plan = get_plan(fault_id, errno_override).as_json()
action_syscalls = {
    "alloc-mballoc": 285,       # fallocate
    "free-punch": 285,          # fallocate
    "mkdir": 258,               # mkdirat
    "rmdir": 84,                # rmdir
    "unlink": 263,              # unlinkat
    "evict-close": 3,           # close
    "evict-close-prev": 3,       # close with a second live orphan
    "xattr-evict-close": 3,     # close
    "xattr-single-evict-close": 3,
    "rename": 316,              # renameat2 on x86_64
    "rename-exchange": 316,
    "rename-dir-inline": 316,
    "rename-whiteout": 316,
    "rename-dir": 316,
    "rename-replace": 316,
    "extent-split": 285,
    "extent-grow-depth": 285,
    "resize-extend": 16,        # ioctl EXT4_IOC_RESIZE_FS
    "resize-add-group": 16,
    "migrate-extents": 16,       # ioctl EXT4_IOC_MIGRATE
    "xattr-set": 188,             # setxattr
    "ea-ref-set": 188,
    "acl-mode": 188,              # setxattr(system.posix_acl_access)
    "inline-dir": 258,            # mkdirat
    "inline-file-write": 1,        # write
    "fileattr-flags": 16,         # ioctl FS_IOC_SETFLAGS
    "fileattr-project": 16,       # ioctl FS_IOC_FSSETXATTR
}
action = plan["action"]
if action not in action_syscalls:
    raise SystemExit(
        f"unsupported fault action for existing C fixture: {action!r} "
        f"(fault plan {fault_id!r}); supported actions: "
        + ", ".join(sorted(action_syscalls)))
if plan.get("syscall_nr") != action_syscalls[action]:
    raise SystemExit(
        f"fault plan {fault_id!r} expects syscall {plan.get('syscall_nr')!r} "
        f"for action {action!r}, but the existing C fixture performs "
        f"syscall {action_syscalls[action]}")
expected = plan.get("expected_return") or {}
if expected.get("errno") not in (0, 5, 12, 22, 28, 30, 117):
    raise SystemExit(
        f"unsupported expected errno {expected.get('errno')!r} in plan "
        f"{fault_id!r}; update the native syscall expectation explicitly")
if plan.get("errno") not in [-code for code in errno_codes(plan["operation"])]:
    raise SystemExit(
        f"injected errno {plan.get('errno')!r} is not a source-listed return "
        f"for hook {plan['operation']!r}")
recovery = plan.get("recovery") or {}
if recovery.get("status") not in ("specified", "verified"):
    raise SystemExit(
        f"fault plan {fault_id!r} has no concrete recovery contract; refusing VM run")

# The fixture sets its comm to this value before arming. Kernel matching uses
# operation, PID, comm, and the zero-based matching-hook call index.
plan["program"] = "fault-formula"
plan["call_index"] = 0
plan["fixture_supported"] = True
print(json.dumps(plan, separators=(",", ":")))
PY
)

plan_fields=$(python3 - "$plan_json" <<'PY'
import json
import sys

plan = json.loads(sys.argv[1])
expected = plan.get("expected_return") or {}
print("\t".join((plan["action"], plan["operation"],
                  str(plan["syscall_nr"]), str(expected.get("result")),
                  str(expected.get("errno")), str(plan["errno"]))))
PY
)
IFS=$'\t' read -r action operation syscall_nr expected_result expected_errno injected_errno <<< "$plan_fields"
repair_kind=$(python3 - "$plan_json" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["recovery"]["repair"]["kind"])
PY
)

if (( dry_plan )); then
	python3 - "$plan_json" <<'PY'
import json
import sys

plan = json.loads(sys.argv[1])
print(json.dumps({
    "fault_id": plan["fault_id"],
    "operation": plan["operation"],
    "action": plan["action"],
    "syscall_nr": plan["syscall_nr"],
    "injected_errno": plan["errno"],
    "expected_return": plan["expected_return"],
    "fixture_supported": plan["fixture_supported"],
}, indent=2, ensure_ascii=False))
PY
	exit 0
fi
printf 'FAULT FORMULA PLAN id=%s operation=%s action=%s syscall_nr=%s injected_errno=%s expected_return=%s/%s control=%s\n' \
	"$fault_id" "$operation" "$action" "$syscall_nr" "$injected_errno" \
	"$expected_result" "$expected_errno" "$control"

if [[ "$build_kernel" == 1 ]]; then
	bash "$project/scripts/build.sh"
fi

run=${run:-$(mktemp -d "$project/results/fault-formula-single-XXXXXXXX")}
mkdir -p "$run"
if find "$run" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
	echo "run directory must be empty to avoid overwriting evidence or disk images: $run" >&2
	exit 1
fi
mkdir -p "$run/evidence"
run=$(cd "$run" && pwd)
evidence="$run/evidence"
raw_guest="$run/fault-formula"
key="$root/work/debian.id_rsa"
kernel="$project/build/linux-counter-trace/arch/x86/boot/bzImage"
base_image="$root/work/debian-clean-smalljournal.img"
disk_image="$run/test.raw"
recovery_image="$run/recovery.raw"
qemu_pidfile="$run/qemu.pid"
guest_bin="$run/fault-formula-single"

# Do not boot a stale image if ext4 sources changed after the last kernel build.
python3 - "$project/dev/linux-6.12.1/fs/ext4" "$kernel" <<'PY'
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
image = pathlib.Path(sys.argv[2])
newest = max((path.stat().st_mtime for path in source.rglob("*")
              if path.is_file() and path.suffix in {".c", ".h"}), default=0)
if not image.is_file() or newest > image.stat().st_mtime:
    raise SystemExit(
        f"kernel image is missing or older than ext4 sources: {image}; "
        "rebuild with --build-kernel")
PY

test -s "$kernel" || {
	echo "missing kernel image: $kernel (use --build-kernel after the source change)" >&2
	exit 1
}
test -s "$base_image"
test -s "$key"
command -v gcc >/dev/null
command -v qemu-img >/dev/null
command -v qemu-system-x86_64 >/dev/null
command -v ssh >/dev/null
command -v scp >/dev/null
command -v mke2fs >/dev/null
command -v e2fsck >/dev/null
command -v sha256sum >/dev/null

python3 - "$plan_json" "$evidence/fault-plan.json" <<'PY'
import json
import pathlib
import sys

plan = json.loads(sys.argv[1])
pathlib.Path(sys.argv[2]).write_text(
    json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
PY

gcc -O2 -Wall -Wextra -Werror -static -o "$guest_bin" \
	"$project/tests/fault-formula-single.c"

qemu-img create -q -f qcow2 -F raw -b "$base_image" "$run/root.qcow2"
disk_size=200M
if [[ "$action" == resize-extend || "$action" == resize-add-group ]]; then
	# Base filesystem is still created at 40,000 blocks; spare device capacity
	# lets the fixture exercise online group extension/add through the ioctl.
	disk_size=400M
fi
qemu-img create -q -f raw "$disk_image" "$disk_size"
format_features='orphan_file'
mount_options=''
case "$action" in
	ea-ref-set)
		format_features='orphan_file,ea_inode'
		;;
	inline-dir|rename-dir-inline)
		format_features='inline_data,orphan_file'
		;;
	inline-file-write)
		format_features='inline_data,orphan_file'
		;;
	fileattr-project)
		format_features='project,quota,orphan_file'
		mount_options='prjquota'
		;;
esac
# The old linked-list orphan hooks require an image without orphan_file.
legacy_orphan=$(python3 - "$plan_json" <<'PY'
import json, sys
print("1" if json.loads(sys.argv[1]).get("requires_legacy_orphan_list") else "0")
PY
)
if [[ "$legacy_orphan" == 1 ]]; then
	mke2fs -q -F -t ext4 -O ^orphan_file -b 4096 -N 16384 \
		-E lazy_itable_init=0 "$disk_image" 40000
else
	mke2fs -q -F -t ext4 -O "$format_features" -b 4096 -N 16384 \
	-E lazy_itable_init=0 "$disk_image" 40000
fi
set +e
e2fsck -fn "$disk_image" > "$run/baseline-fsck.txt" 2>&1
baseline_fsck_rc=$?
set -e
if (( baseline_fsck_rc > 1 )); then
	echo "baseline e2fsck failed (exit=$baseline_fsck_rc); see $run/baseline-fsck.txt" >&2
	exit 1
fi
printf 'FAULT FORMULA PREPARED run_dir=%s disk=%s\n' "$run" "$disk_image"

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 1 \
	-kernel "$kernel" -drive "file=$run/root.qcow2,format=qcow2,if=virtio" \
	-drive "file=$disk_image,format=raw,if=virtio" \
	-netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$port-:22" \
	-device virtio-net-pci,netdev=n0 \
	-append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
	-display none -monitor none -serial "file:$run/serial.log" \
	-daemonize -pidfile "$qemu_pidfile"
qemu_pid=$(<"$qemu_pidfile")
cleanup() {
	if [[ -s "$qemu_pidfile" ]]; then
		kill "$qemu_pid" 2>/dev/null || true
	fi
}
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
printf 'FAULT FORMULA VM READY pid=%s run_dir=%s\n' "$qemu_pid" "$run"
scp -q "${scp_opts[@]}" "$guest_bin" \
	"$project/tests/guest-fault-formula-single.sh" root@127.0.0.1:/tmp/
guest_control_arg=''
if (( control )); then
	guest_control_arg=' --control'
fi

set +e
ssh "${ssh_opts[@]}" root@127.0.0.1 \
	"sh /tmp/guest-fault-formula-single.sh --fault-id '$fault_id' --operation '$operation' --action '$action' --errno '$injected_errno' --mount-options '$mount_options' --fixture /tmp/fault-formula-single --evidence-dir /tmp/fault-formula$guest_control_arg" \
	> "$run/guest.txt" 2>&1
guest_rc=$?
set -e
printf 'FAULT FORMULA WORKLOAD END guest_rc=%s\n' "$guest_rc"
set +e
scp -qr "${scp_opts[@]}" root@127.0.0.1:/tmp/fault-formula "$run/"
fetch_rc=$?
set -e
if (( fetch_rc != 0 )); then
	echo "guest evidence fetch failed (exit=$fetch_rc)" > "$run/fetch-error.txt"
	else
	mkdir -p "$evidence/guest-raw"
	cp -a "$raw_guest/." "$evidence/guest-raw/"
fi
ssh "${ssh_opts[@]}" root@127.0.0.1 'systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
	kill -0 "$qemu_pid" 2>/dev/null || break
	sleep 1
done
if kill -0 "$qemu_pid" 2>/dev/null; then
	echo 'guest did not power off cleanly; offline recovery checks unavailable' > "$run/fsck.txt"
	fsck_rc=99
	kill "$qemu_pid" 2>/dev/null || true
	for _ in $(seq 1 10); do
		kill -0 "$qemu_pid" 2>/dev/null || break
		sleep 1
	done
	printf '{"valid":false,"status":"unavailable","error":"qemu_still_running"}\n' \
		> "$run/disk.json"
	disk_rc=99
else
	# Preserve the guest-produced image as the primary artifact.  All repair
	# operations below run on this copy so fsck cannot erase the original
	# on-disk evidence.
	cp --reflink=auto --preserve=all "$disk_image" "$recovery_image"
	primary_hash=$(sha256sum "$disk_image" | awk '{print $1}')
	recovery_before_hash=$(sha256sum "$recovery_image" | awk '{print $1}')
	if [[ "$primary_hash" != "$recovery_before_hash" ]]; then
		echo "recovery copy hash mismatch" >&2
		exit 1
	fi
	set +e
	e2fsck -fn "$disk_image" > "$run/fsck-before-recovery.txt" 2>&1
	pre_recovery_fsck_rc=$?
	set -e
	set +e
	python3 "$project/scripts/check-disk.py" "$disk_image" \
		--output "$run/disk-before-recovery.json" \
		> /dev/null 2> "$run/disk-before-recovery-error.txt"
	pre_recovery_disk_rc=$?
	set -e
	if (( (pre_recovery_fsck_rc != 0 || pre_recovery_disk_rc != 0) )) && \
		[[ "$repair_kind" == e2fsck-fy ]]; then
		set +e
		e2fsck -fy "$recovery_image" > "$run/fsck-recovery.txt" 2>&1
		recovery_rc=$?
		set -e
	else
		set +e
		e2fsck -fn "$recovery_image" > "$run/fsck-recovery.txt" 2>&1
		recovery_rc=$?
		set -e
	fi
	set +e
	e2fsck -fn "$recovery_image" > "$run/fsck.txt" 2>&1
	fsck_rc=$?
	set -e
 	set +e
	python3 "$project/scripts/check-disk.py" "$recovery_image" --output "$run/disk.json" \
		> /dev/null 2> "$run/disk-error.txt"
	disk_rc=$?
	set -e
	primary_after_checks_hash=$(sha256sum "$disk_image" | awk '{print $1}')
	recovery_after_repair_hash=$(sha256sum "$recovery_image" | awk '{print $1}')
	recovery_after_checks_hash="$recovery_after_repair_hash"
	repair_performed=false
	if [[ "$repair_kind" == e2fsck-fy && \
		"$recovery_before_hash" != "$recovery_after_repair_hash" ]]; then
		repair_performed=true
	fi
	python3 - "$run/image-integrity.json" "$primary_hash" \
		"$primary_after_checks_hash" "$recovery_before_hash" \
		"$recovery_after_repair_hash" "$recovery_after_checks_hash" \
		"$repair_performed" <<'PY'
import json
import pathlib
import sys

path, primary_guest, primary_checks, recovery_before, recovery_repair, \
    recovery_checks, repair_performed = sys.argv[1:]
payload = {
    "schema": 1,
    "repair_target": "recovery.raw",
    "repair_performed": repair_performed == "true",
    "primary_image": {
        "path": "../test.raw",
        "sha256_after_guest": primary_guest,
        "sha256_after_checks": primary_checks,
    },
    "recovery_image": {
        "path": "../recovery.raw",
        "sha256_after_guest": primary_guest,
        "sha256_before_repair": recovery_before,
        "sha256_after_repair": recovery_repair,
        "sha256_after_checks": recovery_checks,
    },
}
pathlib.Path(path).write_text(json.dumps(payload, indent=2) + "\n")
PY
fi
printf 'FAULT FORMULA OFFLINE fsck_rc=%s disk_rc=%s\n' "$fsck_rc" "$disk_rc"

set +e
python3 "$project/scripts/collect-fault-formula-evidence.py" "$project" "$run" \
	--guest-exit "$guest_rc" --fsck-exit "$fsck_rc" --disk-exit "$disk_rc" \
	--pre-recovery-fsck-exit "${pre_recovery_fsck_rc:-99}" \
	--pre-recovery-disk-exit "${pre_recovery_disk_rc:-99}" \
	--recovery-exit "${recovery_rc:-99}" \
	$( (( control )) && printf '%s' --control )
collect_rc=$?
set -e
if (( collect_rc != 0 )); then
	echo "evidence collection failed (exit=$collect_rc)" >&2
	exit "$collect_rc"
fi
set +e
if (( control )); then
	python3 "$project/scripts/validate-fault-control.py" "$evidence" \
		--output "$evidence/control-validation.json"
	validation_rc=$?
	cp "$evidence/control-validation.json" "$run/fault-control.json"
else
	python3 "$project/scripts/fault_evidence_v2.py" "$evidence" \
		--output "$evidence/validation.json"
	validation_rc=$?
	cp "$evidence/validation.json" "$run/fault-evidence.json"
fi
set -e
if (( control )); then
	classification=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["classification"])' \
		"$evidence/control-validation.json")
else
	classification=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["classification"])' \
		"$evidence/validation.json")
fi
printf 'fault_formula_single_worker=complete\nrun_dir=%s\nclassification=%s\n' \
	"$run" "$classification"
exit "$validation_rc"
