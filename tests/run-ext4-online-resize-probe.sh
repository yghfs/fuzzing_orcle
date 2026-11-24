#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${root_dir}/tests/ext4-normal-env.sh"
work_dir="${root_dir}/work"
base_image="${SYZ_BASE_IMAGE:-${ext4_normal_guest_image}}"
port="${SYZ_PORT:-49341}"
run_dir="${work_dir}/ext4-online-resize-probe-$(date +%Y%m%d%H%M%S)-$$"
disk="${run_dir}/disk.raw"
overlay="${run_dir}/root.qcow2"
pid_file="${run_dir}/qemu.pid"
serial_log="${run_dir}/serial.log"
probe_kind="${SYZ_PROBE_KIND:-resize}"
case "${probe_kind}" in
    resize) resize_blocks="${SYZ_RESIZE_BLOCKS:-786432}" ;;
    group-add|fsmap) resize_blocks="${SYZ_RESIZE_BLOCKS:-0}" ;;
    *) printf 'Unknown probe: %s\n' "${probe_kind}" >&2; exit 2 ;;
esac
ssh_opts=(-i "${work_dir}/debian.id_rsa" -p "${port}" -o BatchMode=yes
    -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

ext4_require_normal_kernel "${FUZZING_ORCLE_BZIMAGE}"
mkdir -p "${run_dir}"
qemu-img create -q -f qcow2 -F raw -b "${base_image}" "${overlay}"
qemu-img create -q -f raw "${disk}" 4G
mke2fs -q -F -t ext4 -b 4096 -N 16384 -O ^orphan_file -E lazy_itable_init=0 "${disk}" 2G
gcc -O2 -Wall -Wextra -static -o "${run_dir}/ext4-online-resize-checker" \
    "${root_dir}/tests/ext4-online-resize-checker.c"
gcc -O2 -Wall -Wextra -static -o "${run_dir}/ext4-group-add-probe" \
    "${root_dir}/tests/ext4-group-add-probe.c"
gcc -O2 -Wall -Wextra -static -o "${run_dir}/ext4-fsmap-probe" \
    "${root_dir}/tests/ext4-fsmap-probe.c"

cleanup() {
    if [[ -s "${pid_file}" ]]; then
        kill "$(<"${pid_file}")" 2>/dev/null || true
    fi
}
trap cleanup EXIT

qemu-system-x86_64 -machine q35,accel=tcg -cpu max -m 1536 -smp 2 \
    -kernel "${FUZZING_ORCLE_BZIMAGE}" \
    -drive "file=${overlay},format=qcow2,if=virtio" \
    -drive "file=${disk},format=raw,if=virtio" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${port}-:22" \
    -device virtio-net-pci,netdev=n0 \
    -append "root=/dev/vda rw console=ttyS0 net.ifnames=0 biosdevname=0 ext4.ext4_checker_panic=0 ext4.ext4_checker_event_cap=4096" \
    -display none -monitor none -serial "file:${serial_log}" -daemonize \
    -pidfile "${pid_file}"

for _ in $(seq 1 90); do
    if ssh "${ssh_opts[@]}" root@127.0.0.1 \
        'test -r /sys/kernel/debug/ext4/vda/invariant_summary' >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
ssh "${ssh_opts[@]}" root@127.0.0.1 \
    'set -e; mkdir -p /mnt/ext4-resize; mount /dev/vdb /mnt/ext4-resize; test -r /sys/kernel/debug/ext4/vdb/invariant_summary'
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${run_dir}/ext4-online-resize-checker" root@127.0.0.1:/root/
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${run_dir}/ext4-group-add-probe" root@127.0.0.1:/root/
scp -q -i "${work_dir}/debian.id_rsa" -P "${port}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${run_dir}/ext4-fsmap-probe" root@127.0.0.1:/root/

set +e
if [[ "${probe_kind}" == group-add ]]; then
    resize_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
        "/root/ext4-group-add-probe /mnt/ext4-resize ${resize_blocks}")"
elif [[ "${probe_kind}" == fsmap ]]; then
    resize_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
        "/root/ext4-fsmap-probe /mnt/ext4-resize ${resize_blocks}")"
else
    resize_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
        "/root/ext4-online-resize-checker /mnt/ext4-resize ${resize_blocks}")"
fi
resize_rc=$?
summary_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
    'cat /sys/kernel/debug/ext4/vdb/invariant_summary')"
summary_rc=$?
dmesg_output="$(ssh "${ssh_opts[@]}" root@127.0.0.1 \
    'dmesg | egrep -i "KASAN:|BUG:|WARNING:|Oops|EXT4-fs.*error|general protection|lockup|hung task" | tail -80 || true')"
dmesg_rc=$?
ssh "${ssh_opts[@]}" root@127.0.0.1 'umount /mnt/ext4-resize && systemctl poweroff' >/dev/null 2>&1
poweroff_rc=$?
for _ in $(seq 1 60); do
    if [[ -s "${pid_file}" ]] && ! kill -0 "$(<"${pid_file}")" 2>/dev/null; then
        break
    fi
    sleep 1
done
if [[ -s "${pid_file}" ]] && kill -0 "$(<"${pid_file}")" 2>/dev/null; then
    printf 'guest did not stop; offline fsck skipped\n' > "${run_dir}/e2fsck.txt"
    fsck_rc=125
else
    cp --reflink=auto "${disk}" "${run_dir}/recovered.raw"
    e2fsck -E journal_only -y "${run_dir}/recovered.raw" > "${run_dir}/journal-replay.txt" 2>&1
    journal_rc=$?
    e2fsck -fn "${run_dir}/recovered.raw" > "${run_dir}/e2fsck.txt" 2>&1
    fsck_rc=$?
fi
set -e
{
    printf '%s\n' "resize_rc=${resize_rc}" "${resize_output}"
    printf '%s\n' "summary_rc=${summary_rc}" "${summary_output}"
    printf '%s\n' "dmesg_rc=${dmesg_rc}" "${dmesg_output}"
    printf '%s\n' "poweroff_rc=${poweroff_rc}" "fsck_rc=${fsck_rc}"
    if [[ -v journal_rc ]]; then printf 'journal_rc=%s\n' "${journal_rc}"; fi
    cat "${run_dir}/e2fsck.txt"
} | tee "${run_dir}/report.txt"

cp "${serial_log}" "${run_dir}/serial.log.final" 2>/dev/null || true
echo "online resize probe complete: ${run_dir}"
(( resize_rc == 0 && summary_rc == 0 && fsck_rc == 0 ))
rg -qx 'violations_total=0' <<< "${summary_output}"
