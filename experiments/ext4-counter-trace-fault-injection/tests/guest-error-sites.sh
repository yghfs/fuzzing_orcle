#!/bin/sh
set -eu
trace=/sys/kernel/tracing
control=/sys/kernel/debug/ext4_error_sites
output=/tmp/error-site-results
mkdir -p "$trace" "$output" /mnt/error-sites
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
mount -t tracefs tracefs "$trace" 2>/dev/null || true
test -e "$control/stats"
echo 0 > "$trace/tracing_on"
echo 0 > "$control/enabled"
echo global > "$trace/trace_clock"
echo 16384 > "$trace/buffer_size_kb"
echo 1 > "$trace/events/ext4_error/error_site/enable"
echo 1 > "$trace/events/ext4_counter/ext4_counter_event/enable"
uname -r > "$output/kernel.txt"
cat "$control/stats" > "$output/stats-disabled.tsv"

stable()
{
  for attempt in $(seq 1 50); do
    cat "/sys/fs/ext4/$1/counter_snapshot" > "$2"
    if grep -q '"valid":true' "$2"; then return 0; fi
    sleep 0.1
  done
  return 1
}

for pair in regular:vdb inline:vdc; do
  kind=${pair%:*}
  dev=${pair#*:}
  echo 1 > "$control/enabled"
  echo 0 > "$trace/events/ext4_error/error_site/filter"
  echo > "$trace/trace"
  # Capture only the mount process for error-site pairing; journal workers
  # and root-filesystem background tasks keep their own independent probes.
  sh -c 'echo "common_pid == $$" > "$1/events/ext4_error/error_site/filter"; echo 1 > "$1/tracing_on"; exec mount "/dev/$2" /mnt/error-sites' sh "$trace" "$dev"
  echo 0 > "$trace/tracing_on"
  cat "$trace/trace" > "$output/$kind-mount.trace"
  for cpu in "$trace"/per_cpu/cpu*; do
    cat "$cpu/stats" > "$output/$kind-mount-$(basename "$cpu")-stats.txt"
  done
  stable "$dev" "$output/$kind-before.json"
  echo > "$trace/trace"
  /tmp/error-site-workload /mnt/error-sites "$trace" > "$output/$kind-workload.txt" 2>&1
  echo 0 > "$trace/tracing_on"
  cat "$trace/trace" > "$output/$kind-workload.trace"
  for cpu in "$trace"/per_cpu/cpu*; do
    cat "$cpu/stats" > "$output/$kind-workload-$(basename "$cpu")-stats.txt"
  done
  echo 0 > "$trace/events/ext4_error/error_site/filter"
  stable "$dev" "$output/$kind-after.json"
  cat "$control/stats" > "$output/$kind-stats.tsv"
  umount /mnt/error-sites
done

echo 0 > "$control/enabled"
cat "$control/stats" > "$output/stats-before-disabled-workload.tsv"
echo > "$trace/trace"
mount /dev/vdb /mnt/error-sites
/tmp/error-site-workload /mnt/error-sites "$trace" > "$output/disabled-workload.txt" 2>&1
cat "$trace/trace" > "$output/disabled-workload.trace"
umount /mnt/error-sites
cat "$control/stats" > "$output/stats-after-disabled-workload.tsv"
dmesg > "$output/dmesg.txt"
echo error_site_smoke=passed
