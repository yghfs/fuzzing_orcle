#!/usr/bin/env python3
"""Reproduce the six inventory defects with realistic_review.csv injections.

Carriers
  efi       kernel-side error-site injection at a CSV site id (formula gated)
  blkdebug  QEMU block-layer EIO hot-swapped in once the guest is mounted

Every case boots twice on private copies of the same base image:

  control   no injection   -> counter_snapshot valid, e2fsck -fn returns 0
  fault     injection armed -> snapshot mismatch and/or e2fsck -fn non-zero

The EFI carrier needs a stack-context hash that only exists after a run, so the
control boot doubles as the discovery pass: its efi_site trace names the
(site, occurrence, context) triple that the fault boot then plans.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
PROJECT = pathlib.Path(__file__).resolve().parents[1]
KERNEL = PROJECT / "build/linux-error-site-trace/arch/x86/boot/bzImage"
ROOTFS = ROOT / "work/debian-normal.img"
KEY = ROOT / "work/debian.id_rsa"
DEV = "vdb"


# ------------------------------------------------------------------- QEMU I/O


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 30
        while True:
            try:
                self.sock.connect(str(path))
                break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
        self.file = self.sock.makefile("rwb", buffering=0)
        self.read()
        self.seq = 0
        self.command("qmp_capabilities")

    def read(self):
        while True:
            line = self.file.readline()
            if not line:
                raise RuntimeError("QMP disconnected")
            data = json.loads(line)
            if "event" not in data:
                return data

    def command(self, name, arguments=None):
        self.seq += 1
        request = {"execute": name, "id": self.seq}
        if arguments is not None:
            request["arguments"] = arguments
        self.file.write(json.dumps(request).encode() + b"\r\n")
        response = self.read()
        if response.get("error"):
            raise RuntimeError(f"QMP {name}: {response['error']}")
        return response.get("return")

    def close(self):
        self.file.close()
        self.sock.close()


class Guest:
    """One QEMU boot on private image copies, driven over SSH."""

    def __init__(self, run, image, port, mount_opts=""):
        self.run = run
        self.image = image
        self.port = port
        self.mount_opts = mount_opts
        self.proc = None
        self.qmp = None
        self.sock_dir = None
        self.log = (run / "serial.log").open("w")
        self.opts = ["-i", str(KEY), "-p", str(port), "-o", "BatchMode=yes",
                     "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
        self.disks = []

    def boot(self):
        run = self.run
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                        "-b", str(ROOTFS), str(run / "root.qcow2")], check=True)
        for index in range(3):
            disk = run / f"disk{index}.img"
            subprocess.run(["cp", "--sparse=always", str(self.image), str(disk)],
                           check=True)
            self.disks.append(disk)
        # AF_UNIX paths cap at 108 bytes, so the control socket lives in /tmp.
        self.sock_dir = pathlib.Path(tempfile.mkdtemp(prefix="repro-qmp-"))
        qmp_path = self.sock_dir / "qmp.sock"
        cmd = [
            "qemu-system-x86_64", "-machine", "q35,accel=tcg", "-cpu", "qemu64",
            "-m", "2048", "-smp", "2", "-kernel", str(KERNEL),
            # Explicit node names pin vda=root and vdb=target; relying on
            # -drive/implicit ordering lets the target take over vda.
            "-blockdev", json.dumps({
                "driver": "qcow2", "node-name": "rootdisk",
                "file": {"driver": "file",
                         "filename": str(run / "root.qcow2")}}),
            "-device", "virtio-blk-pci,drive=rootdisk,id=vda",
            # vote-threshold equal to the child count makes a single
            # unvoted (EIO) read fail the whole request instead of letting
            # the healthy sibling win the vote.
            "-blockdev", json.dumps({
                "driver": "quorum", "node-name": "q0",
                "read-pattern": "quorum", "vote-threshold": 2,
                "children": [
                    {"driver": "raw", "node-name": "c0",
                     "file": {"driver": "file", "filename": str(self.disks[0])}},
                    {"driver": "raw", "node-name": "c1",
                     "file": {"driver": "file", "filename": str(self.disks[1])}},
                ],
            }),
            "-device", "virtio-blk-pci,drive=q0,id=vdb",
            "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.port}-:22",
            "-device", "virtio-net-pci,netdev=n0",
            # nokaslr: the EFI plan keys on a stack-trace hash of absolute kernel
            # text addresses, so a relocating kernel would invalidate every
            # plan discovered in the control boot.
            "-append", "root=/dev/vda rw console=ttyS0 net.ifnames=0 nokaslr",
            "-display", "none", "-monitor", "none",
            "-serial", "file:" + str(run / "console.log"),
            "-qmp", f"unix:{qmp_path},server=on,wait=off",
        ]
        (run / "qemu-command.json").write_text(json.dumps(cmd, indent=2) + "\n")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        self.qmp = QMP(qmp_path)

    def ssh(self, text, timeout=180, check=True):
        result = subprocess.run(["ssh", *self.opts, "root@127.0.0.1", text],
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"guest command failed: {text}\n"
                               f"{result.stdout}\n{result.stderr}")
        return result

    def scp(self, local, remote):
        opts = [v if v != "-p" else "-P" for v in self.opts]
        subprocess.run(["scp", "-q", *opts, str(local), f"root@127.0.0.1:{remote}"],
                       check=True, timeout=180)

    def wait_ready(self, timeout=300):
        """Wait for the kernel, then mount the target and confirm the checker."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("QEMU exited during boot")
            # debugfs must be up before efi_init can open the session file.
            result = self.ssh(
                "mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true; "
                "test -e /sys/kernel/debug/ext4_efi/session", check=False)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise TimeoutError("guest never exposed the EFI session")
        # counter_snapshot exists only while the target device is mounted.
        self.ssh("mkdir -p /mnt/p && mount "
                 f"{self.mount_opts} /dev/vdb /mnt/p")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.ssh(f"test -e /sys/fs/ext4/{DEV}/counter_snapshot",
                        check=False).returncode == 0:
                return
            time.sleep(0.5)
        raise TimeoutError("guest never exposed counter_snapshot")

    def arm_fault(self, sector, iotype):
        """Swap one quorum child for a blkdebug node returning EIO.

        QEMU refuses blockdev-reopen for blkdebug config changes, so the
        fault node is built here with its rules already loaded and then
        swapped in through the quorum node, which does accept child changes.
        """
        config = self.run / "blkdebug.conf"
        # once="off": ext4 re-reads the same bitmap twice on the extend path
        # (buddy load + wait_block_bitmap); with once="on" the second read
        # succeeds and the syscall returns 0, hiding the failure entirely.
        config.write_text(
            '[inject-error]\n'
            'event = "none"\n'
            f'iotype = "{iotype}"\n'
            f'sector = "{sector}"\n'
            'errno = "5"\n'
            'once = "off"\n'
        )
        self.qmp.command("blockdev-add", {
            "driver": "file", "node-name": "ff", "filename": str(self.disks[2])})
        self.qmp.command("blockdev-add", {
            "driver": "raw", "node-name": "rr", "file": "ff"})
        self.qmp.command("blockdev-add", {
            "driver": "blkdebug", "node-name": "fault", "image": "rr",
            "config": str(config)})
        self.qmp.command("x-blockdev-change", {"parent": "q0", "node": "fault"})
        self.qmp.command("x-blockdev-change",
                         {"parent": "q0", "child": "children.1"})

    def start_trace(self):
        self.ssh("echo 0 > /sys/kernel/tracing/tracing_on; "
                 ": > /sys/kernel/tracing/trace; "
                 "echo 1 > /sys/kernel/tracing/events/ext4_efi/efi_site/enable; "
                 "echo 1 > /sys/kernel/tracing/events/ext4_efi/efi_formula/enable; "
                 "echo 1 > /sys/kernel/tracing/tracing_on")
        # Zero the runtime model counters so only this workload's error exits
        # are reported. Mount-time and prepare-time work would otherwise blur
        # the evidence for a single-syscall window.
        self.ssh(f"test -w /sys/kernel/debug/ext4/{DEV}/invariant_reset && "
                 f"echo 1 > /sys/kernel/debug/ext4/{DEV}/invariant_reset",
                 check=False)

    def stop_trace(self, path):
        self.ssh("echo 0 > /sys/kernel/tracing/tracing_on; "
                 "cat /sys/kernel/tracing/trace > /tmp/trace.txt", check=False)
        path.write_text(self.ssh("cat /tmp/trace.txt", check=False).stdout)

    def snapshot(self, path):
        self.ssh(
            "for i in $(seq 1 60); do "
            f"cat /sys/fs/ext4/{DEV}/counter_snapshot > /tmp/snap.json 2>/dev/null || true; "
            "grep -q '\"valid\":true' /tmp/snap.json 2>/dev/null && "
            "grep -q '\"namespace_valid\":true' /tmp/snap.json && "
            "grep -q '\"relations_valid\":true' /tmp/snap.json && break; "
            "sleep 0.1; done", check=False)
        path.write_text(self.ssh("cat /tmp/snap.json", check=False).stdout)

    def invariant_summary(self):
        """Runtime model counters from the checker's own debugfs node.

        counter_snapshot refuses to compare anything once the fs is marked
        unhealthy (EXT4_ERROR_FS), which a real block-layer EIO always does.
        The per-model violation counters are recorded at the error exits
        themselves and are therefore the channel that can catch these defects
        while the EIO is still in flight.
        """
        text = self.ssh(f"cat /sys/kernel/debug/ext4/{DEV}/invariant_summary",
                        check=False).stdout
        return dict(re.findall(r"^(\w+)=(\d+)$", text, re.MULTILINE))

    def shutdown(self):
        if self.proc is None:
            self.log.close()
            return
        self.ssh("umount /mnt/p 2>/dev/null; sync; systemctl poweroff",
                 check=False, timeout=90)
        try:
            self.proc.wait(timeout=150)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)
        if self.qmp is not None:
            self.qmp.close()
        self.log.close()
        if self.sock_dir is not None:
            shutil.rmtree(self.sock_dir, ignore_errors=True)


# ---------------------------------------------------------------- EFI carrier


def parse_sites(path):
    events = []
    for line in path.read_text(errors="replace").splitlines():
        if "efi_site:" not in line:
            continue
        fields = dict(re.findall(r"(\w+)=(\S+)", line.split("efi_site:", 1)[1]))
        if fields.get("phase") != "enter":
            continue
        events.append({k: int(v) for k, v in fields.items()
                       if k.isidentifier() and v.lstrip("-").isdigit()})
    return events


def site_hits(trace, site):
    return [e for e in trace if e.get("site_id") == site
            and e.get("eligible") and e.get("allowed")]


# --------------------------------------------------------------- image layout


def make_image(path, blocks, blocksize, inodes, file_blocks=None, features=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate((file_blocks or blocks) * blocksize)
    subprocess.run(["mke2fs", "-q", "-F", "-t", "ext4",
                    "-O", features or "^orphan_file", "-b", str(blocksize),
                    "-N", str(inodes), "-E", "lazy_itable_init=0",
                    str(path), str(blocks)], check=True)


def prepare_image(image, spec):
    """Apply the pre-state offline so the whole image is byte-identical.

    Building the pre-state inside the guest is wrong twice over: the writes
    only reach the two quorum children (never disk2, which the swap later
    installs), and the victim inode stays live in the dentry cache, so its
    itable buffer is pinned and never re-read -- the blkdebug rule would have
    nothing to fail. Doing it with debugfs keeps every copy identical and
    leaves the victim's inode-table block cold on disk.
    """
    if not (spec.get("prep_dir") or spec.get("prep_file") or spec.get("prep_fill")):
        return
    temp = None
    commands = []
    if spec.get("prep_fill"):
        # Burn inodes so the syscall's own allocation lands in an itable block
        # the readahead bio for the parent's block does not reach.
        commands.append("mkdir /d")
        temp = pathlib.Path(tempfile.mkstemp(prefix="repro-prep-")[1])
        temp.write_bytes(b"x")
        commands += [f"write {temp} /d/f{n}" for n in range(spec["prep_fill"])]
    if spec.get("prep_dir"):
        commands.append(f"mkdir {spec['prep_dir']}")
    if spec.get("prep_file"):
        if temp is None:
            temp = pathlib.Path(tempfile.mkstemp(prefix="repro-prep-")[1])
            temp.write_bytes(b"x")
        commands.append(f"write {temp} {spec['prep_file']}")
    try:
        subprocess.run(["debugfs", "-w", "-f", "-", str(image)],
                       input="\n".join(commands) + "\n", text=True,
                       check=True, capture_output=True)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def image_inode(image, name):
    """Inode number debugfs assigned to a path we created offline."""
    result = subprocess.run(["debugfs", "-R", f"stat {name}", str(image)],
                            capture_output=True, text=True, check=True)
    match = re.search(r"Inode:\s*(\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"cannot resolve inode for {name}: {result.stdout}")
    return int(match.group(1))


def layout(path):
    """Per-group block bitmap locations plus block/inode geometry."""
    env = dict(os.environ, LC_ALL="C")
    text = subprocess.run(["dumpe2fs", str(path)], capture_output=True,
                          text=True, check=True, env=env).stdout
    groups, table, bitmap = {}, None, None
    for line in text.splitlines():
        match = re.match(r"Group (\d+):", line)
        if match:
            if table is not None:
                groups[int(match.group(1)) - 1] = {"table": table,
                                                   "bitmap": bitmap}
            table = bitmap = None
            continue
        if table is None and "Inode table at" in line:
            table = int(re.search(r"Inode table at (\d+)", line).group(1))
        if bitmap is None and "Block bitmap at" in line:
            bitmap = int(re.search(r"Block bitmap at (\d+)", line).group(1))
    if table is not None:
        groups[len(groups)] = {"table": table, "bitmap": bitmap}
    header = subprocess.run(["dumpe2fs", "-h", str(path)], capture_output=True,
                            text=True, check=True, env=env).stdout
    size = int(re.search(r"Block size:\s+(\d+)", header).group(1))
    isize = int(re.search(r"Inode size:\s+(\d+)", header).group(1))
    ipg = int(re.search(r"Inodes per group:\s+(\d+)", header).group(1))
    return {"groups": groups, "blocksize": size, "per_block": size // isize,
            "inodes_per_group": ipg}


def inode_block(table, per_block, ino):
    return table + (ino - 1) // per_block


def locate_inode(geo, ino):
    """Block number holding this inode, following the group's inode table."""
    ipg = geo["inodes_per_group"]
    group = (ino - 1) // ipg
    index = (ino - 1) % ipg
    return geo["groups"][group]["table"] + index // geo["per_block"]


# ------------------------------------------------------------------- one boot


def boot_and_run(args, spec, geo, stage, plan, sector, iotype, run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    guest = Guest(run_dir, spec["image"], args.port, spec.get("mount_opts", ""))
    try:
        guest.boot()
        guest.wait_ready()
        guest.scp(PROJECT / "tests/repro-efi-workload", "/tmp/fixture")
        if spec.get("mount_prepare"):
            guest.ssh(f"set -e; cd /mnt/p; {spec['mount_prepare']}; sync")
        guest.ssh("sync")
        guest.start_trace()
        guest.snapshot(run_dir / "snapshot-before.json")
        if plan:
            (run_dir / "plan.txt").write_text(plan)
            guest.scp(run_dir / "plan.txt", "/tmp/plan.txt")
        # The inode-table sector only applies to the block-layer carrier; an
        # EFI run must not also arm blkdebug or the two fault models would be
        # active at once and the result could not be attributed to either.
        if (stage == "fault" and args.carrier == "blkdebug"
                and spec.get("fault_ino") and sector is None):
            block = locate_inode(geo, spec["fault_ino"])
            sector = block * (geo["blocksize"] // 512)
            print(f"  fault inode={spec['fault_ino']} block={block} "
                  f"sector={sector}", flush=True)
        if sector is not None:
            # Arm before dropping the cache: reading counter_snapshot pulls the
            # group bitmaps back into the page cache, and a cached bitmap means
            # the syscall issues no I/O and never sees the injected EIO.
            guest.arm_fault(sector, iotype)
        guest.ssh("sync; echo 1 > /proc/sys/vm/drop_caches")
        plan_env = "EFI_PLAN=/tmp/plan.txt" if plan else "EFI_PLAN="
        argv = " ".join(str(v) for v in spec["argv"])
        guest.ssh(f"cd /mnt/p; EFI_RUN_ID=1 {plan_env} EFI_RESULT=/tmp/session.txt "
                  f"/tmp/fixture {spec['mode']} /mnt/p {argv} > /tmp/workload.txt 2>&1; "
                  f"echo $? > /tmp/rc.txt", check=False)
        guest.snapshot(run_dir / "snapshot-after.json")
        # Collect the model counters before the trace stops: they are the only
        # checker channel that survives EXT4_ERROR_FS.
        summary = guest.invariant_summary()
        (run_dir / "invariant_summary.txt").write_text(
            "\n".join(f"{k}={v}" for k, v in summary.items()) + "\n")
        spec["violations"] = {
            k: int(v) for k, v in summary.items()
            if k.endswith("_violations") and int(v)}
        guest.stop_trace(run_dir / "trace.txt")
        for name in ("workload.txt", "rc.txt", "session.txt"):
            run_dir.joinpath(name).write_text(
                guest.ssh(f"cat /tmp/{name}", check=False).stdout)
        guest.ssh("dmesg > /tmp/dmesg.txt", check=False)
        run_dir.joinpath("dmesg.txt").write_text(
            guest.ssh("cat /tmp/dmesg.txt", check=False).stdout)
    finally:
        guest.shutdown()
    # After the swap the guest writes through disk2, so that copy is the
    # authoritative post-workload image; disk0 keeps the pre-swap state.
    target = run_dir / ("disk2.img" if sector is not None else "disk0.img")
    rc, log = e2fsck(target)
    (run_dir / "e2fsck.txt").write_text(log)
    spec["used_sector"] = sector
    return rc, log


def e2fsck(path):
    result = subprocess.run(["e2fsck", "-fn", str(path)], capture_output=True,
                            text=True, timeout=300)
    return result.returncode, result.stdout + result.stderr


# ----------------------------------------------------------------------- main


def evidence(path):
    """The fixture's one-line REPRO_RESULT, so the verdict records the errno."""
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("REPRO_RESULT"):
                return line
        return path.read_text(errors="replace").strip()[:200]
    except OSError:
        return None


def count_io_errors(path):
    try:
        return sum("I/O error" in line for line in path.read_text(errors="replace")
                   .splitlines())
    except OSError:
        return None


def classify(snapshot_path, e2fsck_rc):
    """Turn the stable snapshot and fsck result into a verdict."""
    try:
        snap = json.loads(snapshot_path.read_text())
    except Exception:
        return "snapshot_unreadable", None
    mismatch = bool(snap.get("namespace_mismatched") or
                    snap.get("relations_mismatches") or
                    snap.get("observed_mismatches"))
    valid = bool(snap.get("valid"))
    if mismatch:
        state = "MISMATCH"
    elif not valid:
        state = f"invalid({snap.get('reason')})"
    else:
        state = "valid"
    if e2fsck_rc:
        state += "+fsck"
    return state, snap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=["group-extend", "group-add", "mkdir",
                                         "rmdir", "unlink", "rename"])
    parser.add_argument("--carrier", choices=["efi", "blkdebug"], required=True)
    parser.add_argument("--site", type=int)
    parser.add_argument("--sector", type=int)
    parser.add_argument("--iotype", choices=["read", "write"], default="read")
    parser.add_argument("--hit", type=int, default=0,
                        help="index into the control-trace hits for --site")
    parser.add_argument("--features",
                        help="mke2fs -O override, e.g. '^orphan_file,^has_journal'")
    parser.add_argument("--mount-opt",
                        help="extra mount options, e.g. '-o sync'")
    parser.add_argument("--label",
                        help="suffix for the result directory, so runs that "
                             "use different sites keep separate artifacts")
    parser.add_argument("--port", type=int, default=11895)
    args = parser.parse_args()
    if args.carrier == "efi" and not args.site:
        parser.error("--carrier efi needs --site")

    spec = build_spec(args)
    label = f"-{args.label}" if args.label else ""
    work = PROJECT / "results" / f"repro-{args.case}-{args.carrier}{label}"
    make_image(spec["image"], **spec["geometry"])
    prepare_image(spec["image"], spec)
    if spec.get("fault_path"):
        # Resolve the inode offline: the fixture's target is fixed by the base
        # image, so the sector can be computed before the guest ever boots.
        spec["fault_ino"] = image_inode(
            spec["image"], spec["fault_path"].replace("/mnt/p", "") or "/")
        spec["resolved_inode"] = spec["fault_ino"]
    geo = layout(spec["image"])
    if args.carrier == "blkdebug" and spec["fault_block"] == "bitmap":
        block = geo["groups"][spec["fault_group"]]["bitmap"]
        args.sector = block * (geo["blocksize"] // 512)
        print(f"  fault block={block} group={spec['fault_group']} "
              f"blocksize={geo['blocksize']}", flush=True)
    elif args.carrier == "blkdebug":
        print(f"  fault inode={spec.get('fault_ino')} resolved offline",
              flush=True)
    print(f"image={spec['image'].name} sector={args.sector}", flush=True)

    # control: identical run without any injection
    rc_c, log_c = boot_and_run(args, spec, geo, "control", "", None, None,
                               work / "control")
    state_c, snap_c = classify(work / "control/snapshot-after.json", rc_c)
    print(f"control  snapshot={state_c} e2fsck={rc_c}", flush=True)

    # fault: derive the plan from the control trace when using EFI
    plan = ""
    if args.carrier == "efi":
        hits = site_hits(parse_sites(work / "control/trace.txt"), args.site)
        if not hits:
            print(json.dumps({"case": args.case, "carrier": "efi",
                              "site": args.site,
                              "verdict": "SITE_NOT_REACHED"}, indent=2))
            (work / "verdict.json").write_text(json.dumps(
                {"case": args.case, "verdict": "SITE_NOT_REACHED"}, indent=2) + "\n")
            return 0
        target = hits[args.hit] if args.hit < len(hits) else None
        if target is None:
            print(json.dumps({"case": args.case, "carrier": "efi",
                              "site": args.site, "verdict": "HIT_INDEX_OUT_OF_RANGE",
                              "available": len(hits)}, indent=2))
            (work / "verdict.json").write_text(json.dumps(
                {"case": args.case, "verdict": "HIT_INDEX_OUT_OF_RANGE"},
                indent=2) + "\n")
            return 0
        plan = f"plan 0 {args.site} {target['occurrence']} {target['context']}\n"
        (work / "discovered.json").write_text(json.dumps(hits, indent=2) + "\n")
        print(f"  reached site {args.site} occurrence={target['occurrence']} "
              f"context={target['context']} hit={args.hit}/{len(hits)}", flush=True)

    rc_f, log_f = boot_and_run(args, spec, geo, "fault", plan,
                               args.sector if args.carrier == "blkdebug" else None,
                               args.iotype, work / "fault")
    state_f, snap_f = classify(work / "fault/snapshot-after.json", rc_f)
    print(f"fault    snapshot={state_f} e2fsck={rc_f}", flush=True)

    verdict = {
        "case": args.case,
        "carrier": args.carrier,
        "site": args.site,
        "sector": spec.get("used_sector"),
        "plan": plan.strip() or None,
        "control": {"snapshot": state_c, "e2fsck_rc": rc_c},
        "fault": {"snapshot": state_f, "e2fsck_rc": rc_f},
        "fault_syscall": evidence(work / "fault" / "workload.txt"),
        "fault_io_error_lines": count_io_errors(work / "fault" / "dmesg.txt"),
        "checker_violations": spec.get("violations") or {},
        "resolved_inode": spec.get("resolved_inode"),
        "reproduced": bool(
            rc_f or spec.get("violations")
            or (snap_f and (snap_f.get("namespace_mismatched") or
                            snap_f.get("relations_mismatches")))),
        "control_clean": rc_c == 0 and state_c == "valid",
    }
    (work / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict, indent=2))
    for label, log in (("control", log_c), ("fault", log_f)):
        print(f"--- {label} e2fsck")
        for line in log.splitlines():
            print("   ", line)
    return 0


def build_spec(args):
    """Geometry, pre-state and syscall arguments for the requested case."""
    case = args.case
    label = f"-{args.label}" if args.label else ""
    work = PROJECT / "results" / f"repro-{case}-{args.carrier}{label}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    image = work / "base.raw"
    geometry = {"blocks": 16384, "blocksize": 4096, "inodes": 4096}
    mount_prepare = ""
    prep_dir = prep_file = None
    prep_fill = 0
    argv = []
    fault_path = None       # guest path whose inode-table block gets the EIO
    fault_block = "inode"   # which metadata block the fault targets
    fault_group = 0         # group whose block bitmap is failed
    mode = case
    if args.features:
        # A journal-less image is what makes CSV site 41 reachable: the
        # buffer_uptodate check in __ext4_handle_dirty_metadata only runs on
        # the no-journal path, and only for sync/dirsync inodes.
        geometry["features"] = args.features
    mount_opts = args.mount_opt or ""

    if case == "group-extend":
        # 1 KiB blocks make groups_per_page == 2, the only geometry where the
        # kzalloc at CSV site 337 (guarded by groups_per_page > 1) exists.
        # Extending 12000 -> 16000 keeps the new blocks inside group 1, whose
        # buddy block bitmap the fault arm then fails to read.
        geometry = {"blocks": 12000, "blocksize": 1024, "inodes": 2048,
                    "file_blocks": 32768}
        argv = [16000]
        fault_block = "bitmap"
        fault_group = 1
    elif case == "group-add":
        # Group 1 must already fit on the device: the ioctl verifies the new
        # group's last block is readable before publishing it, so the backing
        # file has to cover 2 x 32768 blocks even though s_blocks_count is
        # still 32768. Covering only the fs itself fails verify_group_input.
        geometry = {"blocks": 32768, "blocksize": 4096, "inodes": 8192,
                    "file_blocks": 65536}
        argv = [1, 32785, 32786, 32787, 32768]
        fault_block = "bitmap"
        fault_group = 1
    elif case == "mkdir":
        # Bug: ext4_inc_count(dir) already ran when ext4_mark_inode_dirty(dir)
        # fails. Parent (2) and the new inode share itable block 41 by default,
        # so the first itable read in the window is the child's own dirty.
        # The filler pushes the new inode ~300 itable blocks past the parent:
        # ext4's readahead pulls several itable blocks per bio (phys_seg 3 at
        # 40 inodes was still inside the window), so the gap has to exceed the
        # readahead run, not just the block.
        prep_fill = 1200
        fault_path = "/mnt/p"
    elif case == "rmdir":
        # Bug: child dirent deleted and child nlink cleared, then the child
        # inode dirty fails and ext4_dec_count(dir) never runs, so the parent
        # keeps its pre-decrement subdirectory link count. The filler moves the
        # child's itable block away from the root's so the injected read is the
        # child's dirty rather than the parent's earlier write.
        prep_fill = 1200
        prep_dir = "/child"
        fault_path = "/mnt/p/child"
    elif case == "unlink":
        # Bug: dirent deleted, then the target inode dirty fails; the exit
        # returns EIO before drop_nlink, so the inode keeps a link count for a
        # name that no longer exists. The filler separates the victim's itable
        # block from the root's so the injected read is the victim's dirty.
        prep_fill = 1200
        prep_file = "/victim"
        fault_path = "/mnt/p/victim"
    elif case == "rename":
        # Bug: destination dirent already inserted when the source inode dirty
        # fails -> both names point at the inode while nlink stays 1. The
        # filler puts the source inode in a later itable block than the root,
        # so the injected read is ext4_mark_inode_dirty(old.inode) rather than
        # the earlier add_dirent_to_buf() parent write.
        prep_fill = 1200
        prep_file = "/src"
        fault_path = "/mnt/p/src"

    return {"image": image, "geometry": geometry, "mount_prepare": mount_prepare,
            "prep_dir": prep_dir, "prep_file": prep_file,
            "prep_fill": prep_fill, "mount_opts": mount_opts,
            "argv": argv, "mode": mode, "fault_path": fault_path,
            "fault_block": fault_block, "fault_group": fault_group}


if __name__ == "__main__":
    sys.exit(main())
