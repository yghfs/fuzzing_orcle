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
    dev=$1; output=$2; attempt=0
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
        sleep 0.05
    done
    return 1
}
checkpoint_capture()
{
    # The target filesystems stay quiescent until the host trace is sealed.
    # In particular, periodic superblock publication must not advance seq
    # between a device's final state read and the global terminal marker.
    frozen_mounts=""
    checkpoint_thaw()
    {
        for frozen in $frozen_mounts; do fsfreeze --unfreeze "$frozen"; done
        frozen_mounts=""
    }
    trap 'checkpoint_thaw' EXIT
    trap 'exit 1' HUP INT TERM
    for pair in $pairs; do
        kind=${pair%:*}
        printf 'CT_PROGRESS phase=CHECKPOINT_FREEZE kind=%s\n' "$kind"
        fsfreeze --freeze "/mnt/ct-$kind"
        frozen_mounts="/mnt/ct-$kind $frozen_mounts"
    done
    for pair in $pairs; do
        kind=${pair%:*}; dev=${pair#*:}
        stable "$dev" "$base/$kind-final.json"
    done
    stats "$base"
    dmesg > "$base/dmesg.txt"
    for pair in $pairs; do
        kind=${pair%:*}; dev=${pair#*:}
        state "$dev" "$base/$kind-final-state.json"
    done
    echo 'CT_CAMPAIGN TERMINAL' > "$trace/trace_marker"
    echo 0 > "$trace/tracing_on"
    checkpoint_thaw
    trap - EXIT HUP INT TERM
    sync
    touch "$base/checkpoint.done"
    for kind in regular bigalloc inline; do umount "/mnt/ct-$kind"; done
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
    init_mode=${4:-standard}
    case "$scenario" in baseline|stateful|large-dir|pressure|all) ;; *) exit 2;; esac
    case "$init_mode" in standard|comparison-seed) ;; *) exit 2;; esac
    if [ "$init_mode" = comparison-seed ] && [ "$scenario" != baseline ]; then
        echo 'comparison seed mode requires baseline scenario' >&2
        exit 2
    fi
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
    echo 1 > "$trace/events/raw_syscalls/sys_enter/enable"
    echo 1 > "$trace/events/raw_syscalls/sys_exit/enable"
    uname -r > "$base/kernel.txt"
    cat /proc/sys/kernel/random/boot_id > "$base/boot-id.txt"
    cat /proc/kallsyms > "$base/kallsyms.txt"
    for pair in $pairs; do
        kind=${pair%:*}; dev=${pair#*:}
        mkdir -p "/mnt/ct-$kind"
        mount -o noatime "/dev/$dev" "/mnt/ct-$kind"
        if [ "$kind" = regular ] && [ "$init_mode" = comparison-seed ]; then
            test -s "/mnt/ct-$kind/syz-work/file0/file0" || {
                echo 'comparison seed image is missing syz-work/file0/file0' >&2
                exit 70
            }
            grep -qx 'counter-trace-seed' "/mnt/ct-$kind/syz-work/file0/file0" || {
                echo 'comparison seed file content mismatch' >&2
                exit 70
            }
            continue
        fi
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
    shift 9; max_batches=$1; source=$2; scenario=${3:-baseline}
    case "$scenario" in baseline|stateful|large-dir|pressure|all) ;; *) exit 2;; esac
    configured_scenario=$(cat "$base/scenario.txt" 2>/dev/null || printf 'baseline\n')
    [ "$scenario" = "$configured_scenario" ] || {
        printf 'scenario_mismatch configured=%s requested=%s\n' "$configured_scenario" "$scenario" > "$base/$id-scenario-error.txt"
        exit 2
    }
    [ "$source" = syzkaller ] || exit 2
    printf 'CT_PROGRESS phase=SNAPSHOT_BEFORE segment=%s kind=%s\n' "$id" "$kind"
    case "$kind" in regular|bigalloc|inline) ;; *) exit 2;; esac
    out="$base/$id"; mkdir "$out" "$out/coverage"
    sharing=private
    if [ "$mode" = concurrent ] && [ "$workers" -gt 1 ]; then sharing=shared; fi
    cat /proc/sys/kernel/random/boot_id > "$out/boot-id.txt"
    for pair in $pairs; do
        k=${pair%:*}; d=${pair#*:}
        state "$d" "$out/$k-start.json"
        stable "$d" "$out/$k-before.json" || { echo 70 > "$out/workload-exit.txt"; stats "$out"; exit 70; }
    done
    started=$(date +%s); batches=0; rc=0
    while [ "$batches" -eq 0 ] || { [ "$batches" -lt "$max_batches" ] && [ "$(date +%s)" -lt "$((started+seconds))" ]; }; do
        current_seed=$((seed+batches)); batches=$((batches+1))
        printf 'batches=%s\nstarted=%s\n' "$batches" "$started" > "$out/progress.txt"
        # timeout kills the process group, including forked workers.
        (ulimit -t "$work_timeout"; ulimit -n 256; ulimit -v 524288
         export CT_CALL_PREFIX="$id-b$batches"
         export CT_COVERAGE_DIR="$out/coverage"
         export CT_KCOV_ENTRIES="${CT_KCOV_ENTRIES:-16777216}"
         export CT_KCOV_FEATURE_SET="${CT_KCOV_FEATURE_SET:-1}"
         export CT_FALLOCATE_MAX_BYTES="${CT_FALLOCATE_MAX_BYTES:-67108864}"
         export CT_IO_MAX_BYTES="${CT_IO_MAX_BYTES:-1048576}"
         export CT_PROGRESS_FD=3
         timeout -k 2 "$work_timeout" /tmp/ct-syz-launcher "/mnt/ct-$kind" "/tmp/ct-syz-$id" "$batches" "$workers" "$memory" "$sharing" \
             3>&1 >> "$out/workload.txt" 2>&1) || rc=$?
        printf 'CT_PROGRESS phase=BATCH_END segment=%s batch=%s exit=%s\n' "$id" "$batches" "$rc"
        echo 'common_pid == 0' > "$trace/events/raw_syscalls/sys_enter/filter"
        echo 'common_pid == 0' > "$trace/events/raw_syscalls/sys_exit/filter"
        [ "$rc" -eq 0 ] || break
    done
    echo "$rc" > "$out/workload-exit.txt"
    printf 'CT_PROGRESS phase=SNAPSHOT_AFTER segment=%s\n' "$id"
    for pair in $pairs; do
        k=${pair%:*}; d=${pair#*:}
        stable "$d" "$out/$k-after.json" || rc=70
    done
    if [ "$sharing" = shared ]; then
        /tmp/ct-syz-launcher --cleanup "/mnt/ct-$kind" || rc=70
        for pair in $pairs; do
            k=${pair%:*}; d=${pair#*:}
            stable "$d" "$out/$k-post-cleanup.json" || rc=70
        done
    fi
    for pair in $pairs; do
        k=${pair%:*}; d=${pair#*:}
        state "$d" "$out/$k-end.json"
    done
    printf 'batches=%s\nstarted=%s\nended=%s\n' "$batches" "$started" "$(date +%s)" > "$out/progress.txt"
    stats "$out"
    dmesg > "$base/dmesg.txt"
    echo "CT_CAMPAIGN END id=$id rc=$rc" > "$trace/trace_marker" || true
    printf 'CT_PROGRESS phase=SEGMENT_END segment=%s exit=%s\n' "$id" "$rc"
    exit "$rc"
    ;;
checkpoint)
    # SSH may disconnect after the terminal marker was written. Make the
    # checkpoint idempotent so a host-side transport retry cannot rerun the
    # stable scans or unmount sequence.
    if [ -e "$base/checkpoint.done" ]; then exit 0; fi
    checkpoint_capture
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
