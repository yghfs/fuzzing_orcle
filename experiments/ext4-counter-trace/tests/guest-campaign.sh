#!/bin/sh
# A continuous host trace_pipe reader owns the trace; guest evidence is bounded.
set -eu
trace=/sys/kernel/tracing
base=/var/lib/ext4-counter-campaign
scenario=baseline
mkdir -p "$trace" "$base"
pairs='regular:vdb bigalloc:vdc inline:vdd'
pressure_target_free_kb=24576
pressure_limit_mb=104
state() { cat "/sys/fs/ext4/$1/counter_trace_state" > "$2"; }
stable()
{
    dev=$1; output=$2; allow_invalid=${3:-0}; attempt=0
    while [ "$attempt" -lt 100 ]; do
        attempt=$((attempt+1))
        printf 'CT_PROGRESS phase=SNAPSHOT_ONE_START dev=%s output=%s\n' "$dev" "$(basename "$output")"
        # The marker and snapshot read share a PID after exec; the parent
        # closes that exact PID window, never a nearby timestamp window.
        sh -c 'echo "CT_PHASE phase=BEGIN source=stable_snapshot target_pid=$$ id=snapshot-$$" > /sys/kernel/tracing/trace_marker; exec cat "$1"' sh "/sys/fs/ext4/$dev/counter_snapshot" > "$output" &
        snapshot_pid=$!
        wait "$snapshot_pid"
        echo "CT_PHASE phase=END source=stable_snapshot target_pid=$snapshot_pid id=snapshot-$snapshot_pid" > "$trace/trace_marker"
        if grep -q '"valid":true' "$output" && grep -q '"namespace_valid":true' "$output" && grep -q '"relations_valid":true' "$output"; then
            printf 'CT_PROGRESS phase=SNAPSHOT_ONE_END dev=%s output=%s\n' "$dev" "$(basename "$output")"
            return 0
        fi
        if [ "$allow_invalid" -eq 1 ] && [ "$attempt" -ge 5 ]; then
            printf 'CT_PROGRESS phase=SNAPSHOT_ONE_UNVERIFIED dev=%s output=%s\n' "$dev" "$(basename "$output")"
            return 0
        fi
        sleep 0.05
    done
    return 1
}
stats()
{
    scenario=$(cat "$base/scenario.txt" 2>/dev/null || printf 'baseline\n')
    for cpu in "$trace"/per_cpu/cpu*; do cat "$cpu/stats" > "$1/$(basename "$cpu")-stats.txt"; done
    cat /proc/meminfo > "$1/meminfo.txt"
    df -k / /mnt/ct-* > "$1/df.txt"
    for pair in $pairs; do
        kind=${pair%:*}
        {
            printf 'scenario=%s\n' "$scenario"
            printf 'stateful_files='
            find "/mnt/ct-$kind/stateful" -type f 2>/dev/null | wc -l
            printf 'large_dir_files='
            find "/mnt/ct-$kind/large-dir" -type f 2>/dev/null | wc -l
            printf 'pressure_bytes='
            stat -c '%s' "/mnt/ct-$kind/.counter-pressure" 2>/dev/null || printf '0\n'
            printf 'available_kb='
            df -Pk "/mnt/ct-$kind" | awk 'NR == 2 {print $4}'
            printf 'pressure_target_free_kb=%s\n' "$pressure_target_free_kb"
        } > "$1/$kind-scenario.txt"
    done
}
fault_reset_kind()
{
    kind=$1
    case "$kind" in
        regular) dev=vdb; options='-O orphan_file' ;;
        bigalloc) dev=vdc; options='-O bigalloc,orphan_file -C 65536' ;;
        inline) dev=vdd; options='-O inline_data,orphan_file' ;;
        *) return 2 ;;
    esac
    mountpoint="/mnt/ct-$kind"
    printf 'CT_PROGRESS phase=FAULT_RESET_START kind=%s dev=%s\n' "$kind" "$dev"
    # Formatting creates a fresh counter epoch and a large amount of helper
    # activity.  Keep that maintenance transaction out of the workload trace
    # by disabling the workload events while leaving tracing_on set.  Turning
    # tracing_on off can make trace_pipe close, which the host would treat as
    # trace loss during an otherwise successful reset.
    reset_events=""
    for event in \
        "$trace/events/ext4_counter/ext4_counter_event/enable" \
        "$trace/events/ext4_fault/ext4_fault_inject/enable" \
        "$trace/events/raw_syscalls/sys_enter/enable" \
        "$trace/events/raw_syscalls/sys_exit/enable"; do
        if [ -e "$event" ]; then
            echo 0 > "$event"
            reset_events="$reset_events $event"
        fi
    done
    sync
    if mountpoint -q "$mountpoint"; then
        umount "$mountpoint" || return 1
    fi
    mkfs.ext4 -q -F -t ext4 -b 4096 -N 4096 $options "/dev/$dev" || return 1
    mkdir -p "$mountpoint"
    mount -o noatime "/dev/$dev" "$mountpoint" || return 1
    mkdir -p "$mountpoint/syz-work"
    for n in 0 1 2 3; do
        mkdir -p "$mountpoint/syz-work/file$n"
        printf 'seed-%s-%s\n' "$kind" "$n" > "$mountpoint/syz-work/file$n/data"
    done
    scenario=$(cat "$base/scenario.txt" 2>/dev/null || printf 'baseline\n')
    if [ "$scenario" = stateful ] || [ "$scenario" = all ]; then
        mkdir -p "$mountpoint/stateful"
        for n in $(seq 0 31); do
            printf 'retained-%s-%s\n' "$kind" "$n" > "$mountpoint/stateful/retained-$n"
        done
    fi
    if [ "$scenario" = large-dir ] || [ "$scenario" = all ]; then
        mkdir -p "$mountpoint/large-dir"
        for n in $(seq 0 255); do
            printf 'large-dir-%s-%s\n' "$kind" "$n" > "$mountpoint/large-dir/entry-$n"
        done
    fi
    sync
    printf '{"schema":1,"kind":"%s","dev":"%s","scenario":"%s","reset":true}\n' \
        "$kind" "$dev" "$scenario" > "$base/fault-reset-$kind.json"
    for event in $reset_events; do echo 1 > "$event"; done
    printf 'CT_PROGRESS phase=FAULT_RESET_END kind=%s dev=%s\n' "$kind" "$dev"
    return 0
}
json_number()
{
    sed -n "s/.*\"$2\":\([0-9][0-9]*\).*/\1/p" "$1"
}
fault_load_arm_status()
{
    [ -s "$out/fault-status-arm.json" ] || return 1
    fault_generation=$(json_number "$out/fault-status-arm.json" generation)
    fault_target_pid=$(json_number "$out/fault-status-arm.json" target_pid)
    fault_target_pgid=$(json_number "$out/fault-status-arm.json" target_pgid)
    fault_random_seed=$(json_number "$out/fault-status-arm.json" random_seed)
    [ -n "$fault_generation" ] && [ -n "$fault_target_pid" ] &&
        [ -n "$fault_target_pgid" ] && [ -n "$fault_random_seed" ] || return 1
    fault_arm_ready=1
    return 0
}
fault_status_matches_arm()
{
    status_path=$1
    [ "$fault_arm_ready" -eq 1 ] || return 1
    grep -Eq '"enabled":true' "$status_path" || return 1
    grep -Eq '"mode":"random"' "$status_path" || return 1
    grep -Eq '"operation":"random"' "$status_path" || return 1
    grep -Eq "\"generation\":${fault_generation}([,}])" "$status_path" || return 1
    grep -Eq "\"target_pid\":${fault_target_pid}([,}])" "$status_path" || return 1
    grep -Eq "\"target_pgid\":${fault_target_pgid}([,}])" "$status_path" || return 1
    grep -Eq "\"random_seed\":${fault_random_seed}([,}])" "$status_path" || return 1
    grep -Eq '"max_hits":1([,}])' "$status_path" || return 1
    errno_operation=$(sed -n 's/.*"errno_operation":"\([^"]*\)".*/\1/p' "$status_path")
    case ",$fault_operations," in
        *",$errno_operation,"*) ;;
        *) return 1 ;;
    esac
    grep -Eq '"injected":[1-9][0-9]*' "$status_path"
}
stable_states()
{
    attempt=0; stable_rounds=0
    while [ "$attempt" -lt 30 ]; do
        attempt=$((attempt+1)); unchanged=1
        for pair in $pairs; do
            kind=${pair%:*}; dev=${pair#*:}
            state "$dev" "$base/.$kind-state-current.json"
            if [ ! -s "$base/.$kind-state-previous.json" ] ||
                ! cmp -s "$base/.$kind-state-current.json" "$base/.$kind-state-previous.json"; then
                unchanged=0
            fi
        done
        if [ "$unchanged" -eq 1 ]; then stable_rounds=$((stable_rounds+1)); else stable_rounds=0; fi
        if [ "$stable_rounds" -ge 2 ]; then
            for pair in $pairs; do
                kind=${pair%:*}
                cp "$base/.$kind-state-current.json" "$base/$kind-initial-state.json"
            done
            rm -f "$base/".*-state-current.json "$base/".*-state-previous.json
            return 0
        fi
        for pair in $pairs; do
            kind=${pair%:*}
            cp "$base/.$kind-state-current.json" "$base/.$kind-state-previous.json"
        done
        sleep 1
    done
    return 1
}
case "${1:-}" in
init)
    buffer=$2
    scenario=${3:-baseline}
    case "$scenario" in baseline|stateful|large-dir|pressure|all) ;; *) exit 2;; esac
    printf '%s\n' "$scenario" > "$base/scenario.txt"
    rm -f "$base/checkpoint.done"
    mount -t tracefs tracefs "$trace" 2>/dev/null || true
    mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
    echo 0 > "$trace/tracing_on"
    echo global > "$trace/trace_clock"
    echo "$buffer" > "$trace/buffer_size_kb"
    echo > "$trace/trace"
    echo 'common_pid == 0' > "$trace/events/raw_syscalls/sys_enter/filter"
    echo 'common_pid == 0' > "$trace/events/raw_syscalls/sys_exit/filter"
    echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
    if [ -e "$trace/events/ext4_fault/ext4_fault_inject/enable" ]; then
        echo 1 > "$trace/events/ext4_fault/ext4_fault_inject/enable"
    fi
    echo 1 > "$trace/events/raw_syscalls/sys_enter/enable"
    echo 1 > "$trace/events/raw_syscalls/sys_exit/enable"
    test ! -e /sys/module/ext4/parameters/ext4_fault_mode
    uname -r > "$base/kernel.txt"
    cat /proc/sys/kernel/random/boot_id > "$base/boot-id.txt"
    cat /proc/kallsyms > "$base/kallsyms.txt"
    for pair in $pairs; do
        kind=${pair%:*}; dev=${pair#*:}
        mkdir -p "/mnt/ct-$kind"
        mount -o noatime "/dev/$dev" "/mnt/ct-$kind"
        # Seed stable directory and file names used by syzkaller's generated
        # path arguments. This improves fd/path validity without changing the
        # per-segment workload or its trace filters.
        mkdir -p "/mnt/ct-$kind/syz-work"
        for n in 0 1 2 3; do
            mkdir -p "/mnt/ct-$kind/syz-work/file$n"
            printf 'seed-%s-%s\n' "$kind" "$n" > "/mnt/ct-$kind/syz-work/file$n/data"
        done
        if [ "$scenario" = stateful ] || [ "$scenario" = all ]; then
            mkdir -p "/mnt/ct-$kind/stateful"
            for n in $(seq 0 31); do
                printf 'retained-%s-%s\n' "$kind" "$n" > "/mnt/ct-$kind/stateful/retained-$n"
            done
        fi
        if [ "$scenario" = large-dir ] || [ "$scenario" = all ]; then
            mkdir -p "/mnt/ct-$kind/large-dir"
            for n in $(seq 0 255); do
                printf 'large-dir-%s-%s\n' "$kind" "$n" > "/mnt/ct-$kind/large-dir/entry-$n"
            done
        fi
        if [ "$scenario" = pressure ] || [ "$scenario" = all ]; then
            # Reserve in small increments, stopping at a protected free-space
            # floor or a hard cap so the filesystem remains usable.
            pressure_file="/mnt/ct-$kind/.counter-pressure"
            : > "$pressure_file"
            allocated_mb=0
            available_kb=$(df -Pk "/mnt/ct-$kind" | awk 'NR == 2 {print $4}')
            while [ "$available_kb" -gt "$pressure_target_free_kb" ] && [ "$allocated_mb" -lt "$pressure_limit_mb" ]; do
                next_mb=$((allocated_mb+4))
                [ "$next_mb" -le "$pressure_limit_mb" ] || next_mb=$pressure_limit_mb
                if fallocate -l "${next_mb}M" "$pressure_file" 2>/dev/null; then
                    allocated_mb=$next_mb
                elif dd if=/dev/zero of="$pressure_file" bs=1M seek="$allocated_mb" count=$((next_mb-allocated_mb)) conv=notrunc status=none 2>/dev/null; then
                    allocated_mb=$next_mb
                else
                    break
                fi
                available_kb=$(df -Pk "/mnt/ct-$kind" | awk 'NR == 2 {print $4}')
            done
            available_kb=$(df -Pk "/mnt/ct-$kind" | awk 'NR == 2 {print $4}')
            [ "$available_kb" -le "$pressure_target_free_kb" ] ||
                printf 'CT_SCENARIO_WARNING pressure_target_not_reached kind=%s available_kb=%s cap_mb=%s\n' "$kind" "$available_kb" "$pressure_limit_mb"
        fi
    done
    stats "$base"
    # Preparation itself can update ext4 counters.  Capture the campaign
    # sequence anchors only after all directory scans and pressure setup have
    # settled, including asynchronous journal/lazy-init work.
    stable_states || { echo 'counter state did not settle after scenario setup' >&2; exit 70; }
    ;;
rotate)
    echo 1 > "$trace/tracing_on"
    echo "CT_CAMPAIGN ROTATE id=$2" > "$trace/trace_marker"
    ;;
segment)
    id=$2; kind=$3; mode=$4; seconds=$5; seed=$6; workers=$7; memory=$8; work_timeout=$9
    shift 9; max_batches=$1; source=$2; scenario=${3:-baseline}; fault_config=${4:-off}; fault_isolation=${5:-1}
    case "$scenario" in baseline|stateful|large-dir|pressure|all) ;; *) exit 2;; esac
    case "$fault_isolation" in 0|1) ;; *) exit 2;; esac
    configured_scenario=$(cat "$base/scenario.txt" 2>/dev/null || printf 'baseline\n')
    [ "$scenario" = "$configured_scenario" ] || {
        printf 'scenario_mismatch configured=%s requested=%s\n' "$configured_scenario" "$scenario" > "$base/$id-scenario-error.txt"
        exit 2
    }
    [ "$source" = syzkaller ] || exit 2
    printf 'CT_PROGRESS phase=SNAPSHOT_BEFORE segment=%s kind=%s\n' "$id" "$kind"
    case "$kind" in regular|bigalloc|inline) ;; *) exit 2;; esac
    out="$base/$id"; mkdir "$out" "$out/coverage"
    # Do not reuse a stale controller snapshot from an earlier attempt.
    rm -f "$out"/fault-status-*.json "$out/fault-policy-armed.txt" "$out/fault-attribution.json"
    sharing=private
    if [ "$mode" = concurrent ] && [ "$workers" -gt 1 ]; then sharing=shared; fi
    fault_dev=
    fault_hit=0
    fault_arm_ready=0
    fault_generation=
    fault_target_pid=
    fault_target_pgid=
    fault_random_seed=
    fault_operations=
    if [ "$fault_config" != off ]; then
        touch "$base/random-fault-enabled"
        [ "$workers" -ge 1 ] || {
            echo 'random fault injection requires at least one worker' >&2
            exit 2
        }
        case "$kind" in
            regular) fault_dev=vdb ;;
            bigalloc) fault_dev=vdc ;;
            inline) fault_dev=vdd ;;
            *) exit 2 ;;
        esac
        fault_operations=$(printf '%s\n' "$fault_config" | sed -n 's/.*operations=\([^ ]*\).*/\1/p')
        [ -n "$fault_operations" ] || exit 2
        printf '%s\n' "$fault_config" > "$out/fault-policy.txt"
    fi
    cat /proc/sys/kernel/random/boot_id > "$out/boot-id.txt"
    allow_invalid=0
    [ "$fault_config" = off ] || allow_invalid=1
    for pair in $pairs; do
        k=${pair%:*}; d=${pair#*:}
        state "$d" "$out/$k-start.json"
        stable "$d" "$out/$k-before.json" "$allow_invalid" || { echo 70 > "$out/workload-exit.txt"; stats "$out"; exit 70; }
    done
    started=$(date +%s); batches=0; rc=0
    while [ "$batches" -eq 0 ] || { [ "$batches" -lt "$max_batches" ] && [ "$(date +%s)" -lt "$((started+seconds))" ]; }; do
        current_seed=$((seed+batches)); batches=$((batches+1))
        printf 'batches=%s\nstarted=%s\n' "$batches" "$started" > "$out/progress.txt"
        # timeout kills the process group, including forked workers.
        if [ -n "$fault_dev" ]; then
            (
                ulimit -t "$work_timeout"; ulimit -n 256; ulimit -v 524288
                export CT_CALL_PREFIX="$id-b$batches"
                export CT_COVERAGE_DIR="$out/coverage"
                export CT_FAULT_CONFIG="$fault_config"
                export CT_FAULT_PATH="/sys/fs/ext4/$fault_dev/fault_inject"
                export CT_FAULT_STATUS_PATH="/sys/fs/ext4/$fault_dev/fault_inject_status"
                export CT_FAULT_ARM_EVIDENCE="$out/fault-status-arm.json"
                export CT_FAULT_POLICY_EVIDENCE="$out/fault-policy-armed.txt"
                if [ "$sharing" = private ] && [ "$fault_isolation" -eq 1 ]; then
                    export CT_FAULT_STOP_AFTER_HIT=1
                    export CT_FAULT_STOP_MARKER="$out/fault-stop-after-call"
                fi
                export CT_KCOV_ENTRIES="${CT_KCOV_ENTRIES:-16777216}"
                export CT_KCOV_FEATURE_SET="${CT_KCOV_FEATURE_SET:-1}"
                export CT_FALLOCATE_MAX_BYTES="${CT_FALLOCATE_MAX_BYTES:-67108864}"
                export CT_PROGRESS_FD=3
                exec timeout -k 2 "$work_timeout" /tmp/ct-syz-launcher \
                    "/mnt/ct-$kind" "/tmp/ct-syz-$id" "$batches" "$workers" "$memory" "$sharing"
            ) 3>&1 >> "$out/workload.txt" 2>&1 &
            workload_pid=$!
            while kill -0 "$workload_pid" 2>/dev/null; do
                cat "/sys/fs/ext4/$fault_dev/fault_inject_status" > "$out/fault-status-live.json"
                if [ "$fault_arm_ready" -eq 0 ]; then
                    fault_load_arm_status || { sleep 0.1; continue; }
                fi
                if [ "$fault_hit" -eq 0 ] && fault_status_matches_arm "$out/fault-status-live.json"; then
                    fault_hit=1
                    cp "$out/fault-status-live.json" "$out/fault-status-hit.json"
                    printf 'CT_PROGRESS phase=FAULT_INJECTION_HIT segment=%s device=%s workload_continues=1 status=%s\n' \
                        "$id" "$fault_dev" "$(tr -d '\n' < "$out/fault-status-hit.json")"
                fi
                sleep 0.1
            done
            batch_rc=0
            wait "$workload_pid" || batch_rc=$?
            cat "/sys/fs/ext4/$fault_dev/fault_inject_status" > "$out/fault-status-after.json"
            if [ "$fault_arm_ready" -eq 0 ]; then
                fault_load_arm_status || true
            fi
            if [ "$fault_hit" -eq 0 ] &&
                fault_status_matches_arm "$out/fault-status-after.json"; then
                fault_hit=1
                cp "$out/fault-status-after.json" "$out/fault-status-hit.json"
                printf 'CT_PROGRESS phase=FAULT_INJECTION_HIT segment=%s device=%s workload_continues=1 status=%s\n' \
                    "$id" "$fault_dev" "$(tr -d '\n' < "$out/fault-status-hit.json")"
            fi
            echo off > "/sys/fs/ext4/$fault_dev/fault_inject"
            cat "/sys/fs/ext4/$fault_dev/fault_inject_status" > "$out/fault-status-disabled.json"
            rc=$batch_rc
        else
            (ulimit -t "$work_timeout"; ulimit -n 256; ulimit -v 524288
             export CT_CALL_PREFIX="$id-b$batches"
             export CT_COVERAGE_DIR="$out/coverage"
             export CT_KCOV_ENTRIES="${CT_KCOV_ENTRIES:-16777216}"
             export CT_KCOV_FEATURE_SET="${CT_KCOV_FEATURE_SET:-1}"
             export CT_FALLOCATE_MAX_BYTES="${CT_FALLOCATE_MAX_BYTES:-67108864}"
             export CT_PROGRESS_FD=3
             timeout -k 2 "$work_timeout" /tmp/ct-syz-launcher "/mnt/ct-$kind" "/tmp/ct-syz-$id" "$batches" "$workers" "$memory" "$sharing" \
                 3>&1 >> "$out/workload.txt" 2>&1) || rc=$?
        fi
        printf 'CT_PROGRESS phase=BATCH_END segment=%s batch=%s exit=%s\n' "$id" "$batches" "$rc"
        echo 'common_pid == 0' > "$trace/events/raw_syscalls/sys_enter/filter"
        echo 'common_pid == 0' > "$trace/events/raw_syscalls/sys_exit/filter"
        [ "$rc" -eq 0 ] || break
    done
    echo "$rc" > "$out/workload-exit.txt"
    printf 'CT_PROGRESS phase=SNAPSHOT_AFTER segment=%s\n' "$id"
    for pair in $pairs; do
        k=${pair%:*}; d=${pair#*:}
        stable "$d" "$out/$k-after.json" "$allow_invalid" || rc=70
    done
    if [ "$sharing" = shared ]; then
        /tmp/ct-syz-launcher --cleanup "/mnt/ct-$kind" || rc=70
        for pair in $pairs; do
            k=${pair%:*}; d=${pair#*:}
            stable "$d" "$out/$k-post-cleanup.json" "$allow_invalid" || rc=70
        done
    fi
    for pair in $pairs; do
        k=${pair%:*}; d=${pair#*:}
        state "$d" "$out/$k-end.json"
    done
    if [ "$fault_hit" -eq 1 ] && [ "$sharing" = private ]; then
        # The injected helper return is isolated from later workload calls.
        # Check the affected filesystem while it is unmounted, then remount it
        # so the campaign can reset that kind before the next segment.  Stop
        # only the counter event first: the remount starts a new counter epoch,
        # which is intentionally outside this fault's syscall prefix. Keep
        # tracing_on set so the host trace reader remains connected.
        fault_counter_event="$trace/events/ext4_counter/ext4_counter_event/enable"
        echo 0 > "$fault_counter_event" || true
        sync
        fault_kind=$kind
        fault_mount="/mnt/ct-$fault_kind"
        printf 'CT_PROGRESS phase=FAULT_OFFLINE_CHECK_START segment=%s kind=%s\n' "$id" "$fault_kind"
        fault_unmount_rc=0
        if mountpoint -q "$fault_mount"; then
            umount "$fault_mount" || fault_unmount_rc=$?
        fi
        set +e
        if [ "$fault_unmount_rc" -eq 0 ]; then
            e2fsck -fn "/dev/$fault_dev" > "$out/fault-fsck.txt" 2>&1
            fault_fsck_rc=$?
            mount -o noatime "/dev/$fault_dev" "$fault_mount"
            fault_remount_rc=$?
        else
            printf 'target filesystem was not unmounted\n' > "$out/fault-fsck.txt"
            fault_fsck_rc=255
            fault_remount_rc=255
        fi
        set -e
        printf 'unmount_exit=%s\nfsck_exit=%s\nremount_exit=%s\n' \
            "$fault_unmount_rc" "$fault_fsck_rc" "$fault_remount_rc" > "$out/fault-offline-status.txt"
        printf 'CT_PROGRESS phase=FAULT_OFFLINE_CHECK_END segment=%s kind=%s unmount_exit=%s fsck_exit=%s remount_exit=%s\n' \
            "$id" "$fault_kind" "$fault_unmount_rc" "$fault_fsck_rc" "$fault_remount_rc"
        # Fault isolation temporarily disables this global trace event so the
        # remount and offline check cannot contaminate the fault prefix.  The
        # guest remains alive for later segments, so restore the event before
        # emitting the segment boundary; otherwise every subsequent kind has
        # a trace with no ext4 counter domain until a filesystem reset.
        echo 1 > "$fault_counter_event" || true
        printf 'CT_PROGRESS phase=FAULT_COUNTER_EVENT_REENABLED segment=%s kind=%s\n' \
            "$id" "$fault_kind"
    fi
    printf 'batches=%s\nstarted=%s\nended=%s\n' "$batches" "$started" "$(date +%s)" > "$out/progress.txt"
    stats "$out"
    dmesg > "$base/dmesg.txt"
    echo "CT_CAMPAIGN END id=$id rc=$rc" > "$trace/trace_marker" || true
    printf 'CT_PROGRESS phase=SEGMENT_END segment=%s exit=%s\n' "$id" "$rc"
    exit "$rc"
    ;;
reset)
    fault_reset_kind "$2"
    ;;
checkpoint)
    # SSH may disconnect after the terminal marker was written. Make the
    # checkpoint idempotent so a host-side transport retry cannot rerun the
    # stable scans or unmount sequence.
    if [ -e "$base/checkpoint.done" ]; then exit 0; fi
    allow_invalid=0
    [ ! -e "$base/random-fault-enabled" ] || allow_invalid=1
    for pair in $pairs; do
        kind=${pair%:*}; dev=${pair#*:}
        stable "$dev" "$base/$kind-final.json" "$allow_invalid"
        state "$dev" "$base/$kind-final-state.json"
    done
    stats "$base"
    dmesg > "$base/dmesg.txt"
    echo 'CT_CAMPAIGN TERMINAL' > "$trace/trace_marker"
    echo 0 > "$trace/tracing_on"
    sync
    touch "$base/checkpoint.done"
    for kind in regular bigalloc inline; do umount "/mnt/ct-$kind"; done
    ;;
discard)
    # Host has persisted these files, and removes only guest spool evidence.
    case "$2" in segment-[0-9]*) rm -rf "$base/$2" "/tmp/ct-syz-$2";; *) exit 2;; esac
    ;;
status)
    cat /proc/uptime
    df -Pk "$base"
    ;;
stop)
    echo 0 > "$trace/tracing_on"
    pkill -KILL -f '^/tmp/ct-campaign-workload' || true
    pkill -KILL -f '^/tmp/ct-syz-launcher' || true
    pkill -KILL -f '^/tmp/ct-syz-segment-[0-9]+/b[0-9]+-w[0-9]+/workload' || true
    stats "$base"
    dmesg > "$base/dmesg.txt"
    ;;
*) exit 2;;
esac
