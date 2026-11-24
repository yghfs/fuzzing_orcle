#!/usr/bin/env bash
set -uo pipefail

# Upstream syz-manager baseline runner. The implementation below builds a
# fresh manager configuration and preserves every run in a unique directory.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ROOT_DIR=$(cd -- "$PROJECT_DIR/../.." && pwd)
. "$ROOT_DIR/tools/kernel-paths.sh"
MANAGER=${UPSTREAM_SYZ_MANAGER:-"$ROOT_DIR/tools/src/syzkaller-master/bin/syz-manager"}
SYZKALLER=${UPSTREAM_SYZKALLER:-"$ROOT_DIR/tools/src/syzkaller-master"}
KERNEL_OBJ=${EXT4_CT_KERNEL_OBJ:-"$FUZZING_ORCLE_KERNEL_OBJ"}
KERNEL_SRC=${EXT4_CT_KERNEL_SRC:-"$FUZZING_ORCLE_KERNEL"}
KERNEL=${EXT4_CT_KERNEL:-"$KERNEL_OBJ/arch/x86/boot/bzImage"}
VMLINUX=${EXT4_CT_VMLINUX:-"$KERNEL_OBJ/vmlinux"}
IMAGE=${EXT4_CT_IMAGE:-"$FUZZING_ORCLE_ROOTFS"}
SSHKEY=${EXT4_CT_SSHKEY:-"$ROOT_DIR/work/debian.id_rsa"}
DURATION_SECONDS=3600
WORKDIR=""
OUTPUT=""
HTTP_PORT=11900
SEED=0
PROCS=1
MODE=fuzzing
COVERAGE_INTERVAL_SECONDS=300
STOP_GRACE_SECONDS=60
SEED_PROGRAM=""
TARGET_EXT4_IMAGE=""
MANAGER_DEBUG=${UPSTREAM_MANAGER_DEBUG:-0}
SEED_AS_CORPUS=${UPSTREAM_SEED_AS_CORPUS:-0}
SEED_EXECUTION_REQUIRED=0

usage() {
    cat <<'EOF'
Usage: run-upstream-syzkaller-baseline.sh [options]

Options:
  --duration-seconds N           Fuzzing duration (default: 3600)
  --workdir DIR                  syz-manager state directory
  --output DIR                   Artifact directory
  --http-port N                  Manager HTTP port (default: 11900)
  --seed N                       Record seed; upstream manager has no seed option
  --procs N                      Processes per VM (default: 1)
  --mode fuzzing|smoke-test      Manager mode (default: fuzzing)
  --coverage-interval-seconds N  Checkpoint interval, 0 disables (default: 300)
  --stop-grace-seconds N         SIGINT grace period (default: 60)
  --seed-program PATH             Optional upstream syzkaller .syz seed copied into a private source view
  --target-ext4-image PATH        Raw target image embedded in the seed, recorded for image-identity checks
  --seed-as-corpus                Preload the seed into workdir/corpus.db before manager startup
  --manager-debug                 Dump executor/VM output into manager.log for seed validation

The manager may retain transitive resource/dependency calls in addition to
the 21 requested calls; the generated syscall set is recorded in manager.log.
EOF
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
while (($#)); do
    case "$1" in
        --duration-seconds|--workdir|--output|--http-port|--seed|--procs|--mode|--coverage-interval-seconds|--stop-grace-seconds|--seed-program|--target-ext4-image)
            (($# >= 2)) || { usage >&2; exit 2; }
            key=$1; value=$2
            case "$key" in
                --duration-seconds) DURATION_SECONDS=$value ;;
                --workdir) WORKDIR=$value ;;
                --output) OUTPUT=$value ;;
                --http-port) HTTP_PORT=$value ;;
                --seed) SEED=$value ;;
                --procs) PROCS=$value ;;
                --mode) MODE=$value ;;
                --coverage-interval-seconds) COVERAGE_INTERVAL_SECONDS=$value ;;
                --stop-grace-seconds) STOP_GRACE_SECONDS=$value ;;
                --seed-program) SEED_PROGRAM=$value ;;
                --target-ext4-image) TARGET_EXT4_IMAGE=$value ;;
            esac
            shift 2
            ;;
        --seed-as-corpus)
            SEED_AS_CORPUS=1
            shift
            ;;
        --manager-debug)
            MANAGER_DEBUG=1
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

for spec in \
    "duration-seconds:$DURATION_SECONDS:1" \
    "http-port:$HTTP_PORT:1024" \
    "seed:$SEED:0" \
    "procs:$PROCS:1" \
    "coverage-interval-seconds:$COVERAGE_INTERVAL_SECONDS:0" \
    "stop-grace-seconds:$STOP_GRACE_SECONDS:1"; do
    IFS=: read -r name value minimum <<< "$spec"
    is_uint "$value" && ((value >= minimum)) || {
        echo "$name must be an integer >= $minimum" >&2
        exit 2
    }
done
((HTTP_PORT <= 65535 && PROCS <= 32)) || { echo "invalid port or procs" >&2; exit 2; }
[[ "$MODE" == fuzzing || "$MODE" == smoke-test ]] || { echo "mode must be fuzzing or smoke-test" >&2; exit 2; }

[[ -x "$MANAGER" ]] || { echo "upstream syz-manager not executable: $MANAGER" >&2; exit 2; }
[[ -d "$SYZKALLER" ]] || { echo "upstream syzkaller tree missing: $SYZKALLER" >&2; exit 2; }
[[ -s "$KERNEL" ]] || { echo "kernel bzImage missing: $KERNEL" >&2; exit 2; }
[[ -s "$VMLINUX" ]] || { echo "vmlinux missing: $VMLINUX" >&2; exit 2; }
[[ -d "$KERNEL_SRC" ]] || { echo "kernel source missing: $KERNEL_SRC" >&2; exit 2; }
[[ -s "$IMAGE" ]] || { echo "rootfs image missing: $IMAGE" >&2; exit 2; }
[[ -r "$SSHKEY" ]] || { echo "SSH key missing: $SSHKEY" >&2; exit 2; }
command -v qemu-img >/dev/null 2>&1 || { echo "qemu-img is required for an isolated rootfs overlay" >&2; exit 2; }
if [[ -n "$SEED_PROGRAM" && ! -s "$SEED_PROGRAM" ]]; then
    echo "upstream seed program missing: $SEED_PROGRAM" >&2
    exit 2
fi
if [[ -n "$TARGET_EXT4_IMAGE" && ! -s "$TARGET_EXT4_IMAGE" ]]; then
    echo "target ext4 image missing: $TARGET_EXT4_IMAGE" >&2
    exit 2
fi
if [[ "$SEED_AS_CORPUS" == 1 && -z "$SEED_PROGRAM" ]]; then
    echo "--seed-as-corpus requires --seed-program" >&2
    exit 2
fi
if [[ -n "$SEED_PROGRAM" ]]; then
    SEED_PROGRAM=$(cd -- "$(dirname -- "$SEED_PROGRAM")" && pwd)/$(basename -- "$SEED_PROGRAM")
    # A seed can be consumed as a one-shot candidate without being retained in
    # manager corpus coverage. Keep executor output so target-FS execution is
    # independently auditable for short and long runs.
    MANAGER_DEBUG=1
    SEED_EXECUTION_REQUIRED=1
fi
if [[ -n "$TARGET_EXT4_IMAGE" ]]; then
    TARGET_EXT4_IMAGE=$(cd -- "$(dirname -- "$TARGET_EXT4_IMAGE")" && pwd)/$(basename -- "$TARGET_EXT4_IMAGE")
fi
if [[ -z "$OUTPUT" ]]; then
    OUTPUT="$PROJECT_DIR/results/upstream-baseline-$(date +%Y%m%d-%H%M%S)"
fi
if [[ -e "$OUTPUT" ]]; then
    echo "refusing to overwrite existing output: $OUTPUT" >&2
    exit 2
fi
mkdir -p -- "$OUTPUT"
if [[ -z "$WORKDIR" ]]; then WORKDIR="$OUTPUT/manager-workdir"; fi
if [[ -e "$WORKDIR" ]]; then
    echo "refusing to reuse existing manager workdir: $WORKDIR" >&2
    exit 2
fi
mkdir -p -- "$WORKDIR" "$OUTPUT/coverage-checkpoints"
if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :$HTTP_PORT" | awk 'NF { found=1 } END { exit !found }'; then
    echo "HTTP port $HTTP_PORT is already listening" >&2
    exit 1
fi

# syz-manager runs its own QEMU instance and must never mutate the shared
# campaign backing image.  Keep a per-run qcow2 overlay even though the QEMU
# backend also uses -snapshot; this makes the isolation explicit and gives
# the comparison report a stable image identity.
ROOTFS_OVERLAY="$OUTPUT/root.qcow2"
if ! qemu-img create -q -f qcow2 -F raw -b "$IMAGE" "$ROOTFS_OVERLAY"; then
    echo "failed to create isolated rootfs overlay: $ROOTFS_OVERLAY" >&2
    exit 1
fi

SYZKALLER_RUNTIME="$SYZKALLER"
if [[ -n "$SEED_PROGRAM" ]]; then
    # LoadSeeds reads sys/<target>/test from the configured syzkaller tree.
    # Make a private hard-link view so adding one ext4 seed never changes the
    # upstream checkout used by other campaigns.
    SYZKALLER_RUNTIME="$OUTPUT/syzkaller-seed-view"
    mkdir -p -- "$SYZKALLER_RUNTIME/sys/linux"
    ln -s "$SYZKALLER/bin" "$SYZKALLER_RUNTIME/bin"
    cp -al "$SYZKALLER/sys/linux/." "$SYZKALLER_RUNTIME/sys/linux/"
    # LoadSeeds scans every file in sys/linux/test.  Keep the private view
    # seed-only: copying the upstream test directory would silently add
    # hundreds of unrelated regression programs to an explicit ext4 seed.
    # Removing hard-links here only removes entries from the private view;
    # the upstream checkout remains untouched.
    rm -rf -- "$SYZKALLER_RUNTIME/sys/linux/test"
    mkdir -p -- "$SYZKALLER_RUNTIME/sys/linux/test"
    cp -- "$SEED_PROGRAM" "$SYZKALLER_RUNTIME/sys/linux/test/ext4-counter-seed.syz"
    if [[ -n "$(find "$SYZKALLER_RUNTIME/sys/linux/test" -mindepth 1 -maxdepth 1 \
            -type f ! -name ext4-counter-seed.syz -print -quit)" ]]; then
        echo "failed to isolate upstream seed program view" >&2
        exit 1
    fi
fi

INITIAL_CORPUS_PROGRAM_COUNT=0
SEED_PROGRAM_AS_CORPUS=false
if [[ "$SEED_AS_CORPUS" == 1 ]]; then
    # syz-manager treats files under sys/linux/test as one-shot candidates.
    # Pack the exact same source seed into the manager database so its first
    # execution is part of the persistent corpus and therefore appears in
    # /rawcover and corpus.db coverage.
    SEED_CORPUS_DIR="$OUTPUT/initial-corpus"
    mkdir -p -- "$SEED_CORPUS_DIR"
    cp -- "$SEED_PROGRAM" "$SEED_CORPUS_DIR/ext4-counter-seed.syz"
    SYZ_DB="$SYZKALLER/bin/syz-db"
    [[ -x "$SYZ_DB" ]] || { echo "syz-db not executable: $SYZ_DB" >&2; exit 2; }
    if ! "$SYZ_DB" -os linux -arch amd64 pack "$SEED_CORPUS_DIR" "$WORKDIR/corpus.db" \
            >"$OUTPUT/initial-corpus.stdout" 2>"$OUTPUT/initial-corpus.stderr"; then
        echo "failed to create initial corpus database" >&2
        exit 1
    fi
    INITIAL_CORPUS_PROGRAM_COUNT=1
    SEED_PROGRAM_AS_CORPUS=true
fi

CONFIG="$OUTPUT/manager.json"
export _UPSTREAM_CONFIG="$CONFIG" _UPSTREAM_WORKDIR="$WORKDIR" _UPSTREAM_HTTP_PORT="$HTTP_PORT"
export _UPSTREAM_KERNEL_OBJ="$KERNEL_OBJ" _UPSTREAM_KERNEL="$KERNEL" _UPSTREAM_IMAGE="$ROOTFS_OVERLAY"
export _UPSTREAM_KERNEL_SRC="$KERNEL_SRC"
export _UPSTREAM_SSHKEY="$SSHKEY" _UPSTREAM_SYZKALLER="$SYZKALLER_RUNTIME" _UPSTREAM_PROCS="$PROCS"
python3 - <<'PY'
import json
import os

syscalls = [
    "openat", "openat$dir", "close", "read", "write", "pwrite64", "fallocate",
    "ftruncate", "fsync", "fdatasync", "mkdirat", "rmdir", "unlinkat", "renameat2",
    "linkat", "symlinkat", "fchmod", "setxattr", "fsetxattr", "removexattr", "fremovexattr",
    "syz_mount_image$ext4", "getegid", "geteuid",
]
config = {
    "name": "ext4-upstream-baseline",
    "target": "linux/amd64",
    "http": f"127.0.0.1:{os.environ['_UPSTREAM_HTTP_PORT']}",
    "workdir": os.environ["_UPSTREAM_WORKDIR"],
    "kernel_obj": os.environ["_UPSTREAM_KERNEL_OBJ"],
    "kernel_src": os.environ["_UPSTREAM_KERNEL_SRC"],
    "kernel_build_src": os.environ["_UPSTREAM_KERNEL_OBJ"],
    "image": os.environ["_UPSTREAM_IMAGE"],
    "sshkey": os.environ["_UPSTREAM_SSHKEY"],
    "ssh_user": "root",
    "syzkaller": os.environ["_UPSTREAM_SYZKALLER"],
    "procs": int(os.environ["_UPSTREAM_PROCS"]),
    "sandbox": "none",
    "reproduce": False,
    "run_fsck": False,
    "cover": True,
    "raw_cover": True,
    "preserve_corpus": True,
    "enable_syscalls": syscalls,
    "type": "qemu",
    "vm": {
        "count": 1,
        "kernel": os.environ["_UPSTREAM_KERNEL"],
        "cmdline": "root=/dev/vda rw console=ttyS0 net.ifnames=0 noapic",
        "image_device": "drive if=virtio,format=qcow2,file=",
        "qemu_args": "-machine q35 -accel tcg,thread=multi -cpu qemu64",
        "network_device": "virtio-net-pci",
        "cpu": 1,
        "mem": 1024,
    },
}
with open(os.environ["_UPSTREAM_CONFIG"], "w", encoding="utf-8") as stream:
    json.dump(config, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY

COLLECTOR="$SCRIPT_DIR/collect-upstream-coverage.py"
SEED_VALIDATOR="$SCRIPT_DIR/validate-upstream-seed-execution.py"
cat > "$OUTPUT/run-metadata.json" <<EOF
{
  "schema": 1,
  "baseline": "upstream-syz-manager",
  "mode": "$MODE",
  "duration_seconds": $DURATION_SECONDS,
  "requested_seed": $SEED,
  "seed_applied": false,
  "http": "http://127.0.0.1:$HTTP_PORT",
  "workdir": "$WORKDIR",
  "config": "$CONFIG",
  "kernel": "$KERNEL",
  "kernel_src": "$KERNEL_SRC",
  "vmlinux": "$VMLINUX",
  "image": "$ROOTFS_OVERLAY",
  "rootfs_backing": "$IMAGE",
  "sshkey": "$SSHKEY",
  "syscall_count": 24,
  "requested_syscall_names": ["openat", "openat\u0024dir", "close", "read", "write", "pwrite64", "fallocate", "ftruncate", "fsync", "fdatasync", "mkdirat", "rmdir", "unlinkat", "renameat2", "linkat", "symlinkat", "fchmod", "setxattr", "fsetxattr", "removexattr", "fremovexattr", "syz_mount_image\u0024ext4", "getegid", "geteuid"],
  "coverage_source": "rawcover",
  "coverage_scope": "manager_corpus",
  "requested_syscalls": 24,
  "syscall_selection": "transitive_closure",
  "seed_program": "${SEED_PROGRAM:-}",
  "seed_program_applied": false,
  "seed_program_as_corpus": $SEED_PROGRAM_AS_CORPUS,
  "initial_corpus_program_count": $INITIAL_CORPUS_PROGRAM_COUNT,
  "seed_program_loaded_count": 0,
  "manager_execution_activity_observed_after_seed_load": false,
  "target_filesystem_execution_verified": false,
  "seed_execution_evidence": "",
  "target_ext4_image": "${TARGET_EXT4_IMAGE:-}",
  "target_ext4_image_sha256": "$(if [[ -n "$TARGET_EXT4_IMAGE" ]]; then sha256sum "$TARGET_EXT4_IMAGE" | awk '{print $1}'; fi)",
  "status": "starting"
}
EOF
sha256sum "$KERNEL" "$VMLINUX" "$IMAGE" "$ROOTFS_OVERLAY" "$CONFIG" > "$OUTPUT/input.sha256"
if [[ -n "$SEED_PROGRAM" ]]; then
    sha256sum "$SEED_PROGRAM" >> "$OUTPUT/input.sha256"
fi

MANAGER_LOG="$OUTPUT/manager.log"
manager_args=(-config "$CONFIG" -mode "$MODE" -vv 1)
if [[ "$MANAGER_DEBUG" == 1 ]]; then
    manager_args=(-debug "${manager_args[@]}")
fi
setsid "$MANAGER" "${manager_args[@]}" >"$MANAGER_LOG" 2>&1 &
MANAGER_PID=$!
SNAPSHOT_PID=""
STOPPED=0
INTERRUPTED=0
ENDPOINT="http://127.0.0.1:$HTTP_PORT"

cleanup() {
    if [[ -n "$SNAPSHOT_PID" ]]; then
        kill "$SNAPSHOT_PID" 2>/dev/null || true
        wait "$SNAPSHOT_PID" 2>/dev/null || true
    fi
    if kill -0 "$MANAGER_PID" 2>/dev/null; then
        if ((ready)); then
            collect_checkpoint "interrupted"
        fi
        kill -INT -- "-$MANAGER_PID" 2>/dev/null || kill -INT "$MANAGER_PID" 2>/dev/null || true
        for _wait in $(seq 1 "$STOP_GRACE_SECONDS"); do
            kill -0 "$MANAGER_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$MANAGER_PID" 2>/dev/null; then
            kill -TERM -- "-$MANAGER_PID" 2>/dev/null || kill -TERM "$MANAGER_PID" 2>/dev/null || true
            for _wait in $(seq 1 5); do
                kill -0 "$MANAGER_PID" 2>/dev/null || break
                sleep 1
            done
        fi
        if kill -0 "$MANAGER_PID" 2>/dev/null; then
            kill -KILL -- "-$MANAGER_PID" 2>/dev/null || kill -KILL "$MANAGER_PID" 2>/dev/null || true
        fi
    fi
}
request_stop() { INTERRUPTED=1; }
trap cleanup EXIT
trap request_stop INT TERM

ready=0
seed_loaded=0
for _attempt in $(seq 1 180); do
    ((INTERRUPTED == 0)) || break
    kill -0 "$MANAGER_PID" 2>/dev/null || break
    if python3 "$COLLECTOR" --endpoint "$ENDPOINT" --source rawcover --output "$OUTPUT/coverage-initial.json" \
            --raw-records "$OUTPUT/coverage-initial.jsonl" --curve "$OUTPUT/coverage.tsv" \
            --vmlinux "$VMLINUX" >/dev/null 2>"$OUTPUT/coverage-initial.error"; then
        if [[ -z "$SEED_PROGRAM" ]] || grep -Eq '\bcorpus[[:space:]]*:[[:space:]]*[0-9]+[[:space:]]+\([1-9][0-9]* seeds\)' "$MANAGER_LOG" || { [[ "$SEED_PROGRAM_AS_CORPUS" == true ]] && grep -Eq '\bcorpus[[:space:]]*:[[:space:]]*[1-9][0-9]*[[:space:]]+\([0-9]+ seeds\)' "$MANAGER_LOG"; }; then
            seed_loaded=1
            # Do not label the immediately available endpoint as an initial
            # fuzzing checkpoint before the configured seed is loaded and the
            # manager has started executing programs.
            if [[ -z "$SEED_PROGRAM" ]] || grep -Eq 'exec total=[1-9][0-9]*' "$MANAGER_LOG"; then
                ready=1
                break
            fi
        fi
    fi
    sleep 1
done

collect_checkpoint() {
    local label=$1
    local raw_args=()
    if [[ "$label" == initial || "$label" == final ]]; then
        raw_args=(--raw-records "$OUTPUT/coverage-checkpoints/$label.jsonl")
    fi
    python3 "$COLLECTOR" --endpoint "$ENDPOINT" --source rawcover \
        --output "$OUTPUT/coverage-checkpoints/$label.json" \
        "${raw_args[@]}" \
        --curve "$OUTPUT/coverage.tsv" --vmlinux "$VMLINUX" \
        >"$OUTPUT/coverage-checkpoints/$label.stdout" \
        2>"$OUTPUT/coverage-checkpoints/$label.error" || true
}

if ((ready)); then
    if ((COVERAGE_INTERVAL_SECONDS > 0)); then
        (
            while kill -0 "$MANAGER_PID" 2>/dev/null; do
                sleep "$COVERAGE_INTERVAL_SECONDS"
                kill -0 "$MANAGER_PID" 2>/dev/null || break
                collect_checkpoint "$(date +%Y%m%d-%H%M%S)"
            done
        ) &
        SNAPSHOT_PID=$!
    fi
else
    if [[ -n "$SEED_PROGRAM" ]] && ((seed_loaded == 0)); then
        echo "manager did not load the configured seed before the startup deadline" > "$OUTPUT/coverage.error"
    elif [[ -n "$SEED_PROGRAM" ]]; then
        echo "manager did not report execution activity after loading the configured seed" > "$OUTPUT/coverage.error"
    else
        echo "manager did not expose coverage endpoint before exiting" > "$OUTPUT/coverage.error"
    fi
fi

manager_status=0
if ((ready == 0)); then
    manager_status=1
    kill -INT -- "-$MANAGER_PID" 2>/dev/null || kill -INT "$MANAGER_PID" 2>/dev/null || true
elif [[ "$MODE" == fuzzing ]] && kill -0 "$MANAGER_PID" 2>/dev/null; then
    deadline=$((SECONDS + DURATION_SECONDS))
    while kill -0 "$MANAGER_PID" 2>/dev/null && ((SECONDS < deadline)) && ((INTERRUPTED == 0)); do
        sleep 1
    done
    if kill -0 "$MANAGER_PID" 2>/dev/null; then
        if ((INTERRUPTED == 0)); then
            STOPPED=1
        fi
        collect_checkpoint "final"
        kill -INT -- "-$MANAGER_PID" 2>/dev/null || kill -INT "$MANAGER_PID" 2>/dev/null || true
        for _wait in $(seq 1 "$STOP_GRACE_SECONDS"); do
            kill -0 "$MANAGER_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$MANAGER_PID" 2>/dev/null; then
            kill -TERM -- "-$MANAGER_PID" 2>/dev/null || kill -TERM "$MANAGER_PID" 2>/dev/null || true
            for _wait in $(seq 1 5); do
                kill -0 "$MANAGER_PID" 2>/dev/null || break
                sleep 1
            done
        fi
        if kill -0 "$MANAGER_PID" 2>/dev/null; then
            kill -KILL -- "-$MANAGER_PID" 2>/dev/null || kill -KILL "$MANAGER_PID" 2>/dev/null || true
        fi
    fi
fi
wait "$MANAGER_PID" 2>/dev/null || manager_status=$?
if [[ -n "$SNAPSHOT_PID" ]]; then
    kill "$SNAPSHOT_PID" 2>/dev/null || true
    wait "$SNAPSHOT_PID" 2>/dev/null || true
fi

seed_execution_status=0
SEED_EVIDENCE=""
if ((SEED_EXECUTION_REQUIRED)); then
    SEED_EVIDENCE="$OUTPUT/seed-execution-evidence.json"
    seed_validation_args=(--log "$MANAGER_LOG" --seed-program "$SEED_PROGRAM" --output "$SEED_EVIDENCE")
    if [[ -n "$TARGET_EXT4_IMAGE" ]]; then
        seed_validation_args+=(--target-ext4-image "$TARGET_EXT4_IMAGE")
    fi
    if python3 "$SEED_VALIDATOR" "${seed_validation_args[@]}" \
            >"$OUTPUT/seed-execution-evidence.stdout" \
            2>"$OUTPUT/seed-execution-evidence.stderr"; then
        seed_execution_status=0
    else
        seed_execution_status=1
    fi
fi

status="failed"
if ((INTERRUPTED)); then
    status="interrupted"
elif [[ "$manager_status" == 0 ]] && ((seed_execution_status == 0)) && { [[ "$MODE" == smoke-test ]] || [[ "$STOPPED" == 1 && "$ready" == 1 ]]; }; then
    status="passed"
fi
python3 - "$OUTPUT/run-metadata.json" "$status" "$manager_status" "$STOPPED" "$ready" "$MANAGER_LOG" "$INTERRUPTED" "$seed_execution_status" "$SEED_EVIDENCE" <<'PY'
import json
import pathlib
import re
import sys
import time

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data.update({
    "status": sys.argv[2],
    "manager_exit_code": int(sys.argv[3]),
    "stopped_after_duration": bool(int(sys.argv[4])),
    "coverage_endpoint_ready": bool(int(sys.argv[5])),
    "interrupted": bool(int(sys.argv[7])),
    "seed_execution_status": int(sys.argv[8]),
    "seed_execution_evidence": sys.argv[9],
    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
})
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
log = pathlib.Path(sys.argv[6])
try:
    text = log.read_text(encoding="utf-8", errors="replace")
except OSError:
    text = ""
match = re.search(r"(?m)^syscalls\s+:\s+(\d+)/(\d+)\s*$", text)
if match:
    data.update({
        "actual_enabled_syscall_count": int(match.group(1)),
        "available_syscall_count": int(match.group(2)),
    })
seed_match = re.search(r"(?m)\bcorpus\s+:\s+\d+\s+\((\d+) seeds\)", text)
if seed_match:
    loaded = int(seed_match.group(1))
    data["seed_program_loaded_count"] = loaded
    data["seed_program_applied"] = bool(data.get("seed_program")) and (loaded > 0 or
        bool(data.get("seed_program_as_corpus")) and
        int(data.get("initial_corpus_program_count", 0) or 0) > 0)
    data["manager_execution_activity_observed_after_seed_load"] = bool(data["seed_program_applied"]) and bool(
        re.search(r"(?m)candidates=\d+ corpus=\d+ coverage=\d+ exec total=[1-9]\d*", text))
elif data.get("seed_program_as_corpus"):
    data["seed_program_applied"] = bool(data.get("seed_program")) and \
        int(data.get("initial_corpus_program_count", 0) or 0) > 0
    data["manager_execution_activity_observed_after_seed_load"] = bool(data["seed_program_applied"]) and bool(
        re.search(r"(?m)candidates=\d+ corpus=\d+ coverage=\d+ exec total=[1-9]\d*", text))
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
evidence_path = pathlib.Path(sys.argv[9]) if sys.argv[9] else None
if evidence_path and evidence_path.is_file():
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evidence = {}
    data["target_filesystem_execution_verified"] = bool(
        evidence.get("target_filesystem_execution_verified"))
    data["seed_execution_evidence_status"] = evidence.get("status", "unreadable")
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
printf 'status\tmanager_exit_code\tseed_execution_status\tcoverage_endpoint_ready\tstopped_after_duration\toutput\n%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$status" "$manager_status" "$seed_execution_status" "$ready" "$STOPPED" "$OUTPUT" > "$OUTPUT/summary.tsv"
echo "upstream baseline $status: $OUTPUT"
if ((INTERRUPTED)); then
    exit 130
fi
if ((seed_execution_status != 0)); then
    exit 1
fi
exit "$manager_status"
