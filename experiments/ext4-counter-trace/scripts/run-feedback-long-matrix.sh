#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
RUNS_ROOT="$PROJECT_DIR/results"
RUNNER=${FEEDBACK_MATRIX_RUNNER:-"$SCRIPT_DIR/run-campaign.sh"}
DURATION_SECONDS=43200
TARGET_SYSCALLS=0
SEED=20261023
OUTPUT_DIR=""
RESOURCE_VALID_EVERY=4
FRESH_GENERATE_EVERY=7
SCHEDULER_POLICY=rarity_fair
MUTATION_BUDGET_POLICY=fixed
TARGET_SCOPE=ext4_jbd2
MAX_RUN_MB=9216
MIN_FREE_MB=16384
PORT_BASE=11883
MIN_MEMORY_MB=6144
STARTUP_STAGGER_SECONDS=60
STOP_GRACE_SECONDS=120

usage() {
    cat <<'EOF'
Usage: run-feedback-long-matrix.sh [options]

Runs PC-only, PC+edge, PC+semantic, and PC+edge+semantic concurrently. Each
campaign collects for twelve hours by default, uses one VM worker, and has strict
KCOV validation: a missing, invalid, or saturated sample stops that campaign
at the next segment boundary and makes its result fail validation.

Options:
  --duration-seconds N             Collection time per campaign (default: 43200)
  --target-syscalls N              Minimum generated and verified syscall budget (default: 0, duration only)
  --seed N                         Shared deterministic start-image seed
  --output DIR                     Matrix logs and summary directory
  --resource-valid-every N         Valid resource input period (default: 4)
  --fresh-generate-every N         Fresh Generate period (default: 7)
  --scheduler-policy POLICY        uniform_round_robin|rarity_fair
  --mutation-budget-policy POLICY  fixed|dynamic
  --target-scope SCOPE             all|ext4_jbd2 (default: ext4_jbd2)
  --max-run-mb N                   Per-campaign logical output cap (default: 9216)
  --min-free-mb N                  Shared filesystem free-space reserve (default: 16384)
  --port-base N                    Four consecutive SSH ports (default: 11883)
  --min-memory-mb N                MemAvailable required before launch (default: 6144)
  --startup-stagger-seconds N      Delay between VM starts (default: 60)
  --stop-grace-seconds N           Grace period before forced child stop (default: 120)
EOF
}

while (($#)); do
    case "$1" in
        --duration-seconds|--target-syscalls|--seed|--output|--resource-valid-every|--fresh-generate-every|\
        --scheduler-policy|--mutation-budget-policy|--target-scope|--max-run-mb|--min-free-mb|--port-base|--min-memory-mb|--startup-stagger-seconds|--stop-grace-seconds)
            (($# >= 2)) || { usage >&2; exit 2; }
            key=$1
            value=$2
            case "$key" in
                --duration-seconds) DURATION_SECONDS=$value ;;
                --target-syscalls) TARGET_SYSCALLS=$value ;;
                --seed) SEED=$value ;;
                --output) OUTPUT_DIR=$value ;;
                --resource-valid-every) RESOURCE_VALID_EVERY=$value ;;
                --fresh-generate-every) FRESH_GENERATE_EVERY=$value ;;
                --scheduler-policy) SCHEDULER_POLICY=$value ;;
                --mutation-budget-policy) MUTATION_BUDGET_POLICY=$value ;;
                --target-scope) TARGET_SCOPE=$value ;;
                --max-run-mb) MAX_RUN_MB=$value ;;
                --min-free-mb) MIN_FREE_MB=$value ;;
                --port-base) PORT_BASE=$value ;;
                --min-memory-mb) MIN_MEMORY_MB=$value ;;
                --startup-stagger-seconds) STARTUP_STAGGER_SECONDS=$value ;;
                --stop-grace-seconds) STOP_GRACE_SECONDS=$value ;;
            esac
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
for spec in \
    "duration-seconds:$DURATION_SECONDS:1:43200" \
    "target-syscalls:$TARGET_SYSCALLS:0:10000000" \
    "seed:$SEED:1:2147483647" \
    "resource-valid-every:$RESOURCE_VALID_EVERY:0:1000" \
    "fresh-generate-every:$FRESH_GENERATE_EVERY:0:1000" \
    "max-run-mb:$MAX_RUN_MB:512:102400" \
    "min-free-mb:$MIN_FREE_MB:0:102400" \
    "port-base:$PORT_BASE:1024:65532" \
    "min-memory-mb:$MIN_MEMORY_MB:0:1048576" \
    "startup-stagger-seconds:$STARTUP_STAGGER_SECONDS:0:3600" \
    "stop-grace-seconds:$STOP_GRACE_SECONDS:1:3600"; do
    IFS=: read -r name value minimum maximum <<< "$spec"
    is_uint "$value" && ((value >= minimum && value <= maximum)) || {
        echo "$name must be an integer in [$minimum, $maximum]" >&2
        exit 2
    }
done
[[ "$SCHEDULER_POLICY" == uniform_round_robin || "$SCHEDULER_POLICY" == rarity_fair ]] || {
    echo "unknown scheduler policy: $SCHEDULER_POLICY" >&2; exit 2;
}
[[ "$MUTATION_BUDGET_POLICY" == fixed || "$MUTATION_BUDGET_POLICY" == dynamic ]] || {
    echo "unknown mutation budget policy: $MUTATION_BUDGET_POLICY" >&2; exit 2;
}
[[ "$MUTATION_BUDGET_POLICY" != dynamic || "$SCHEDULER_POLICY" == rarity_fair ]] || {
    echo "dynamic mutation budgets require rarity_fair scheduling" >&2; exit 2;
}
[[ "$TARGET_SCOPE" == all || "$TARGET_SCOPE" == ext4_jbd2 ]] || {
    echo "unknown target scope: $TARGET_SCOPE" >&2; exit 2;
}
[[ -f "$RUNNER" ]] || { echo "campaign runner not found: $RUNNER" >&2; exit 2; }

if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$RUNS_ROOT/feedback-long-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p -- "$OUTPUT_DIR" "$RUNS_ROOT"
SUMMARY="$OUTPUT_DIR/summary.tsv"
if [[ -e "$SUMMARY" ]]; then
    echo "refusing to overwrite existing summary: $SUMMARY" >&2
    exit 2
fi
for label in pc pc_edge pc_semantic pc_edge_semantic; do
    if [[ -e "$OUTPUT_DIR/$label.log" ]]; then
        echo "refusing to overwrite existing log: $OUTPUT_DIR/$label.log" >&2
        exit 2
    fi
done

ports=("$PORT_BASE" "$((PORT_BASE + 1))" "$((PORT_BASE + 2))" "$((PORT_BASE + 3))")
labels=(pc pc_edge pc_semantic pc_edge_semantic)
modes=(pc pc_edge pc_semantic pc_edge_semantic)
if (( $(nproc) < 4 )); then
    echo "parallel matrix requires at least 4 host CPUs" >&2
    exit 1
fi
available_memory_mb=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
if ((available_memory_mb < MIN_MEMORY_MB)); then
    echo "parallel matrix requires ${MIN_MEMORY_MB} MiB MemAvailable; found ${available_memory_mb} MiB" >&2
    exit 1
fi

# Reserve all four campaign budgets up front. Campaigns grow in the same
# results filesystem and each worker also enforces the shared free-space floor.
required_bytes=$(((4 * MAX_RUN_MB + MIN_FREE_MB) * 1024 * 1024))
available_bytes=$(df -B1 --output=avail "$RUNS_ROOT" | tail -n 1 | tr -d '[:space:]')
if ! is_uint "$available_bytes" || ((available_bytes < required_bytes)); then
    echo "insufficient disk space on $RUNS_ROOT: need $required_bytes bytes (four quotas plus reserve), have ${available_bytes:-unknown}" >&2
    exit 1
fi

# Coordinate matrix instances by port and reject ports already owned by an
# unrelated campaign before starting any VM.
PORT_LOCK_DIR="${TMPDIR:-/tmp}/ext4-counter-feedback-matrix-${UID}"
mkdir -p -- "$PORT_LOCK_DIR"
declare -a port_lock_fds=()
for port in "${ports[@]}"; do
    exec {lock_fd}>"$PORT_LOCK_DIR/$port.lock"
    if ! flock -n "$lock_fd"; then
        echo "SSH port $port is reserved by another feedback matrix" >&2
        exit 1
    fi
    if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :$port" | awk 'NF { found=1 } END { exit !found }'; then
        echo "SSH port $port is already listening" >&2
        exit 1
    fi
    port_lock_fds+=("$lock_fd")
done
if ! python3 - "${ports[@]}" <<'PY'
import socket, sys
for value in sys.argv[1:]:
    port = int(value)
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
        except OSError as error:
            print(f'SSH port {port} is unavailable: {error}', file=sys.stderr)
            raise SystemExit(1)
PY
then
    exit 1
fi

printf 'configuration\tfeedback_mode\ttarget_scope\tport\tduration_seconds\ttarget_syscalls\texit_code\tvalidation\tstop_reason\tcompletion_order\tmatrix_action\trun_dir\n' > "$SUMMARY"
printf '[%s] launching four campaigns in parallel; duration=%ss seed=%s\n' \
    "$(date --iso-8601=seconds)" "$DURATION_SECONDS" "$SEED"
printf '[%s] disk available=%s MiB required=%s MiB; MemAvailable=%s MiB; ports=%s-%s\n' \
    "$(date --iso-8601=seconds)" "$((available_bytes / 1024 / 1024))" \
    "$((required_bytes / 1024 / 1024))" "$available_memory_mb" "${ports[0]}" "${ports[3]}"

declare -a pids=() run_logs=() run_labels=("${labels[@]}") run_modes=("${modes[@]}") run_ports=("${ports[@]}") completed=() summary_rows=()
declare -A pid_to_index=()
for label in "${labels[@]}"; do
    run_logs+=("$OUTPUT_DIR/$label.log")
    completed+=(0)
done
matrix_shutdown_requested=0
matrix_shutdown_action=""
matrix_shutdown_reason=""
stop_watchdog_pid=""
completion_order=0
overall_status=0

request_children_stop() {
    local reason=$1 i
    if ((matrix_shutdown_requested == 0)); then
        matrix_shutdown_requested=1
        matrix_shutdown_reason=$reason
    fi
    for i in "${!pids[@]}"; do
        if [[ "${completed[$i]}" != 1 ]]; then
            kill -TERM -- "-${pids[$i]}" 2>/dev/null || kill -TERM "${pids[$i]}" 2>/dev/null || true
        fi
    done
    if [[ -z "$stop_watchdog_pid" ]]; then
        # Put the watchdog and its sleep in a dedicated process group.  A
        # plain background subshell leaves its sleep orphaned when the matrix
        # exits, which can keep the caller's log pipe open for the full grace.
        setsid bash -c 'sleep "$1"; shift; kill -KILL "$@" 2>/dev/null || true' \
            _ "$STOP_GRACE_SECONDS" "${pids[@]}" &
        stop_watchdog_pid=$!
    fi
}

force_children_stop() {
    local i
    for i in "${!pids[@]}"; do
        if [[ "${completed[$i]}" != 1 ]]; then
            kill -KILL -- "-${pids[$i]}" 2>/dev/null || kill -KILL "${pids[$i]}" 2>/dev/null || true
        fi
    done
}

matrix_signal() {
    if ((matrix_shutdown_requested)); then
        printf '[%s] second stop signal received; forcing remaining campaigns\n' \
            "$(date --iso-8601=seconds)" >&2
        force_children_stop
        return
    fi
    matrix_shutdown_action="stopped_by_signal"
    overall_status=1
    request_children_stop "signal_received"
    printf '[%s] stop requested; waiting up to %ss for campaign checkpoints\n' \
        "$(date --iso-8601=seconds)" "$STOP_GRACE_SECONDS" >&2
}
trap 'matrix_signal' INT TERM HUP

# Arrays are initialized before launch so a signal during startup still gets
# one explicit summary row for every configuration.
for i in "${!labels[@]}"; do
    label=${labels[$i]}
    mode=${modes[$i]}
    port=${ports[$i]}
    log="$OUTPUT_DIR/$label.log"
    if ((matrix_shutdown_requested)); then
        break
    fi
    if ((i > 0 && STARTUP_STAGGER_SECONDS > 0)); then
        printf '[%s] waiting %s seconds before next VM startup\n' \
            "$(date --iso-8601=seconds)" "$STARTUP_STAGGER_SECONDS"
        sleep "$STARTUP_STAGGER_SECONDS"
        if ((matrix_shutdown_requested)); then
            break
        fi
    fi
    printf '[%s] starting %s on SSH port %s\n' "$(date --iso-8601=seconds)" "$label" "$port"
    setsid bash "$RUNNER" \
        --mode single \
        --segments 100000 \
        --duration-seconds "$DURATION_SECONDS" \
        --target-syscalls "$TARGET_SYSCALLS" \
        --segment-seconds 1 \
        --max-batches 1 \
        --batch-timeout 30 \
        --boot-timeout 180 \
        --analysis-timeout 180 \
        --workers 1 \
        --memory-mb 0 \
        --guest-memory-mb 1024 \
        --cpus 1 \
        --buffer-kb 8192 \
        --max-trace-mb 32 \
        --max-run-mb "$MAX_RUN_MB" \
        --min-free-mb "$MIN_FREE_MB" \
        --port "$port" \
        --seed "$SEED" \
        --syzkaller-calls 16 \
        --resource-valid-every "$RESOURCE_VALID_EVERY" \
        --fresh-generate-every "$FRESH_GENERATE_EVERY" \
        --scheduler-policy "$SCHEDULER_POLICY" \
        --feedback-mode "$mode" \
        --strict-kcov-feedback \
        --mutation-budget-policy "$MUTATION_BUDGET_POLICY" \
        --target-scope "$TARGET_SCOPE" \
        --scenario baseline >"$log" 2>&1 &
    pids+=("$!")
    pid_to_index["${pids[-1]}"]=$i
done

record_result() {
    local i=$1 rc=$2 action=$3
    local label=${run_labels[$i]} mode=${run_modes[$i]} port=${run_ports[$i]} log=${run_logs[$i]}
    local run_dir validation stop_reason
    run_dir=$(sed -n 's/^run_dir=//p' "$log" 2>/dev/null | tail -n 1)
    validation=execution_failed
    stop_reason=unknown
    if [[ -n "$run_dir" && -f "$run_dir/campaign.json" && -f "$run_dir/campaign-report.json" ]]; then
        stop_reason=$(python3 - "$run_dir" <<'PY'
import json, pathlib, sys
run = pathlib.Path(sys.argv[1])
try:
    meta = json.loads((run / 'campaign.json').read_text())
    report = json.loads((run / 'campaign-report.json').read_text())
except (OSError, ValueError, TypeError):
    raise SystemExit(0)
print(report.get('stop_reason') or meta.get('stop_reason') or 'unknown')
PY
        )
        if [[ "$action" == stopped_by_signal ]]; then
            validation=stopped_by_signal
        elif [[ "$action" == aborted_after_failure ]]; then
            validation=aborted_after_failure
        elif python3 - "$run_dir" "$mode" "$TARGET_SCOPE" "$DURATION_SECONDS" "$TARGET_SYSCALLS" <<'PY'
import json, pathlib, sys
run = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
target_scope = sys.argv[3]
duration = int(sys.argv[4])
target_syscalls = int(sys.argv[5])
try:
    meta = json.loads((run / 'campaign.json').read_text())
    report = json.loads((run / 'campaign-report.json').read_text())
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
limits = meta.get('limits', {})
elapsed = report.get('collection_elapsed_seconds')
expected_stop_reason = 'syscall_budget' if target_syscalls else 'duration_limit'
budget_reached = (
    not target_syscalls or
    int(report.get('generated_syscalls', 0)) >= target_syscalls and
    int(report.get('native_verified',
                   report.get('verified_syscalls',
                              meta.get('verified_syscalls', 0)))) >= target_syscalls
)
elapsed_valid = (
    isinstance(elapsed, (int, float)) and
    (elapsed >= duration * 0.98 if not target_syscalls else elapsed > 0)
)
valid = (
    meta.get('status') == 'complete' and
    limits.get('feedback_mode') == mode and
    limits.get('target_scope', target_scope) == target_scope and
    int(limits.get('target_syscalls', 0)) == target_syscalls and
    limits.get('strict_kcov_feedback') is True and
    report.get('passed') is True and
    report.get('kcov_feedback_complete') is True and
    report.get('coverage_complete') is True and
    report.get('stop_reason') == expected_stop_reason and
    budget_reached and elapsed_valid and
    report.get('segments', 0) > 0 and
    report.get('complete_segments') == report.get('segments') and
    not report.get('kcov_feedback_gaps')
)
raise SystemExit(0 if valid else 1)
PY
        then
            validation=passed
            if ((rc != 0)); then
                validation=runner_failed
            fi
        else
            validation=report_or_feedback_incomplete
            rc=1
        fi
    elif [[ "$action" == stopped_by_signal ]]; then
        validation=stopped_by_signal
    elif [[ "$action" == aborted_after_failure ]]; then
        validation=aborted_after_failure
    fi
    completion_order=$((completion_order + 1))
    summary_rows[$i]=$(printf '%s\t%s\t%s\t%s\t%d\t%d\t%d\t%s\t%s\t%d\t%s\t%s' \
        "$label" "$mode" "$TARGET_SCOPE" "$port" "$DURATION_SECONDS" "$TARGET_SYSCALLS" "$rc" "$validation" "$stop_reason" \
        "$completion_order" "${action:-normal}" "${run_dir:-unknown}")
    printf '[%s] finished %s exit=%d validation=%s stop_reason=%s run_dir=%s\n' \
        "$(date --iso-8601=seconds)" "$label" "$rc" "$validation" "$stop_reason" "${run_dir:-unknown}"
    if ((rc != 0)) || [[ "$validation" != passed ]]; then
        overall_status=1
    fi
    LAST_VALIDATION=$validation
}

pending_pids=("${pids[@]}")
while ((${#pending_pids[@]})); do
    finished_pid=""
    # Only runner PIDs are passed to wait -n; the stop watchdog is also a
    # child but must never be mistaken for a completed campaign.
    wait -n -p finished_pid "${pending_pids[@]}"
    rc=$?
    # wait -n clears the -p variable when a signal interrupts the wait.
    finished_pid="${finished_pid-}"
    if [[ -z "$finished_pid" || -z "${pid_to_index[$finished_pid]+present}" ]]; then
        # Fallback for Bash versions that do not populate -p.
        finished_pid=${pending_pids[0]}
        wait "$finished_pid" 2>/dev/null
        rc=$?
    fi
    i=${pid_to_index[$finished_pid]}
    completed[$i]=1
    next_pending=()
    for pid in "${pending_pids[@]}"; do
        [[ "$pid" != "$finished_pid" ]] && next_pending+=("$pid")
    done
    pending_pids=("${next_pending[@]}")
    action=normal
    if ((matrix_shutdown_requested)); then
        action=${matrix_shutdown_action:-stopped_by_signal}
    fi
    record_result "$i" "$rc" "$action"
    if [[ "$action" == normal && ( "$rc" != 0 || "$LAST_VALIDATION" != passed ) ]]; then
        # A failed mode must not erase valid evidence from the other three
        # independent campaigns. Keep collecting them to their own syscall
        # budget; the matrix still exits nonzero and records this failure.
        matrix_shutdown_action=campaign_failed_but_continued
        overall_status=1
        printf '[%s] %s failed; continuing remaining campaigns independently\n' \
            "$(date --iso-8601=seconds)" "${run_labels[$i]}" >&2
    fi
done

if [[ -n "$stop_watchdog_pid" ]]; then
    kill -TERM -- "-$stop_watchdog_pid" 2>/dev/null || true
    wait "$stop_watchdog_pid" 2>/dev/null || true
fi

# A stop during staggered startup leaves some configurations without a child.
# Record them instead of returning a misleading one-line summary.
for i in "${!labels[@]}"; do
    if [[ "${completed[$i]}" != 1 ]]; then
        completed[$i]=1
        matrix_shutdown_action=${matrix_shutdown_action:-stopped_by_signal}
        record_result "$i" 125 "$matrix_shutdown_action"
    fi
done

# Keep the stable configuration order in the TSV while completion_order
# records the actual wait -n order used for diagnosis.
for i in "${!labels[@]}"; do
    printf '%s\n' "${summary_rows[$i]}" >> "$SUMMARY"
done

trap - INT TERM HUP

printf 'results_dir=%s\nsummary=%s\n' "$OUTPUT_DIR" "$SUMMARY"
exit "$overall_status"
