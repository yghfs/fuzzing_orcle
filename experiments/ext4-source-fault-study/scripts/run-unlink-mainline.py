#!/usr/bin/env python3
"""Reproduce unlink inode-table READ EIO against an unmodified mainline kernel."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
STUDY = ROOT / "experiments/ext4-source-fault-study"
RESULTS = STUDY / "results/unlink-mainline-fresh-clean-20261116"
KERNEL = RESULTS / "linux-mainline.bzImage"
INIT = STUDY / "initramfs/unlink-mainline-init"
IMAGE = RESULTS / "seed-mainline.img"
UPSTREAM_SHA = "aebb7274476403040d582c61478df8f4a7bf9cd3"


class QMP:
    def __init__(self, path, transcript):
        self.transcript = transcript
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
        self.seq = 0
        self.transcript.write(json.dumps({"greeting": self.read()}) + "\n")
        self.command("qmp_capabilities")

    def read(self):
        line = self.file.readline()
        if not line:
            raise RuntimeError("QMP disconnected")
        return json.loads(line)

    def command(self, name, arguments=None):
        self.seq += 1
        request = {"execute": name, "id": self.seq}
        if arguments is not None:
            request["arguments"] = arguments
        self.file.write(json.dumps(request).encode() + b"\r\n")
        self.transcript.write(json.dumps({"request": request}) + "\n")
        while True:
            response = self.read()
            self.transcript.write(json.dumps({"response": response}) + "\n")
            self.transcript.flush()
            if response.get("id") == self.seq:
                if response.get("error"):
                    raise RuntimeError(f"QMP {name}: {response['error']}")
                return response.get("return")

    def close(self):
        self.file.close()
        self.sock.close()


def copy_sparse(src, dst):
    subprocess.run(["cp", "--sparse=always", str(src), str(dst)], check=True)


def wait_serial(sock, needle, transcript, timeout=90):
    sock.settimeout(0.5)
    data = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            break
        data.extend(chunk)
        transcript.write(chunk)
        transcript.flush()
        if needle in data:
            return bytes(data)
    raise TimeoutError(f"serial did not print {needle!r}")


def build_initrd(out, init_script=INIT):
    with tempfile.TemporaryDirectory(prefix="unlink-mainline-initrd-") as temp:
        work = pathlib.Path(temp)
        source = subprocess.Popen(
            ["gzip", "-dc", str(STUDY / "build/source-fault-initramfs.cpio.gz")],
            stdout=subprocess.PIPE,
        )
        subprocess.run(["cpio", "-id", "--quiet"], cwd=work,
                       stdin=source.stdout, check=True)
        source.stdout.close()
        if source.wait() != 0:
            raise RuntimeError("could not unpack source initramfs")
        shutil.copy2(init_script, work / "init")
        (work / "init").chmod(0o755)
        finder = subprocess.Popen(["find", ".", "-print0"], cwd=work,
                                  stdout=subprocess.PIPE)
        cpio = subprocess.Popen(["cpio", "--null", "-o", "-H", "newc", "--quiet"],
                                cwd=work, stdin=finder.stdout, stdout=subprocess.PIPE)
        finder.stdout.close()
        with out.open("wb") as output:
            gzip = subprocess.Popen(["gzip", "-1"], stdin=cpio.stdout, stdout=output)
            cpio.stdout.close()
            if finder.wait() != 0 or cpio.wait() != 0 or gzip.wait() != 0:
                raise RuntimeError("could not build mainline initramfs")


def save_debugfs(image, command, path):
    with path.open("w") as out:
        subprocess.run(["debugfs", "-R", command, str(image)], stdout=out,
                       stderr=subprocess.STDOUT, check=False)


def debugfs_text(image, command):
    result = subprocess.run(["debugfs", "-R", command, str(image)], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            check=False)
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=pathlib.Path, default=IMAGE)
    parser.add_argument("--kernel", type=pathlib.Path, default=KERNEL)
    parser.add_argument("--init", type=pathlib.Path, default=INIT)
    parser.add_argument("--results", type=pathlib.Path, default=RESULTS)
    parser.add_argument("--expect-fixed", action="store_true",
                        help="expect the READ EIO to leave the directory entry intact")
    args = parser.parse_args()
    results = args.results
    image = args.image
    results.mkdir(parents=True, exist_ok=True)
    for required in (args.kernel, args.init, image):
        if not required.is_file():
            raise FileNotFoundError(required)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (results / "seed-image.sha256").write_text(f"{digest}  {image}\n")
    precheck = subprocess.run(["e2fsck", "-fn", str(image)], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (results / "seed-e2fsck.log").write_text(precheck.stdout)
    if precheck.returncode != 0:
        raise RuntimeError("mainline seed image does not pass e2fsck -fn")

    parent_stat = debugfs_text(image, "stat /p")
    target_stat = debugfs_text(image, "stat /p/file")
    parent_match = re.search(r"Inode:\s*(\d+)", parent_stat)
    target_match = re.search(r"Inode:\s*(\d+)", target_stat)
    table_match = re.search(r"located at block (\d+)",
                            debugfs_text(image, f"imap <{parent_match.group(1)}>")) if parent_match else None
    if not (parent_match and target_match and table_match):
        raise RuntimeError("could not resolve /p, /p/file, and parent inode-table block")
    parent_ino = int(parent_match.group(1))
    target_ino = int(target_match.group(1))
    table_block = int(table_match.group(1))
    sector = table_block * 8  # 4 KiB filesystem block, 512-byte sectors
    (results / "resolved-input.txt").write_text(
        f"parent_inode={parent_ino}\ntarget_inode={target_ino}\n"
        f"parent_inode_table_block={table_block}\nread_error_sector={sector}\n")

    initrd = results / "initramfs-mainline.cpio.gz"
    build_initrd(initrd, args.init)
    qmp_transcript = (results / "qmp-transcript.jsonl").open("w")
    serial_log_path = results / "serial.log"
    qemu_log_path = results / "qemu.log"
    with tempfile.TemporaryDirectory(prefix="qemu-unlink-mainline-") as temp:
        run = pathlib.Path(temp)
        disks = [run / f"disk{i}.img" for i in range(3)]
        copy_sparse(image, disks[0])
        copy_sparse(image, disks[1])
        blkconf = run / "parent-sector-2312.blkdebug.conf"
        blkconf.write_text(
            '[inject-error]\n'
            'event = "none"\n'
            'iotype = "read"\n'
            f'sector = "{sector}"\n'
            'errno = "5"\n'
            'once = "on"\n'
        )
        shutil.copy2(blkconf, results / blkconf.name)
        qmp_path = run / "qmp.sock"
        serial_path = run / "serial.sock"
        command = [
            "qemu-system-x86_64", "-machine", "q35", "-m", "512M", "-smp", "2",
            "-display", "none", "-monitor", "none", "-no-reboot",
            "-kernel", str(args.kernel), "-initrd", str(initrd),
            "-append", "console=ttyS0 panic=-1 loglevel=7",
            "-qmp", f"unix:{qmp_path},server=on,wait=off",
            "-chardev", f"socket,id=serial0,path={serial_path},server=on,wait=off",
            "-serial", "chardev:serial0",
            "-blockdev", json.dumps({
                "driver": "quorum", "node-name": "q0", "read-pattern": "quorum",
                "vote-threshold": 2,
                "children": [
                    {"driver": "raw", "file": {"driver": "file", "filename": str(disks[0])}},
                    {"driver": "raw", "file": {"driver": "file", "filename": str(disks[1])}},
                ],
            }),
            "-device", "virtio-blk-pci,drive=q0,id=vda",
        ]
        (results / "qemu-command.json").write_text(json.dumps(command, indent=2) + "\n")
        with qemu_log_path.open("wb") as qemu_log, serial_log_path.open("wb") as serial_log:
            proc = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT)
            qmp = QMP(qmp_path, qmp_transcript)
            try:
                deadline = time.time() + 30
                while not serial_path.exists():
                    if proc.poll() is not None or time.time() >= deadline:
                        raise RuntimeError("QEMU exited before serial socket appeared")
                    time.sleep(0.05)
                serial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                serial.connect(str(serial_path))
                try:
                    wait_serial(serial, b"QMP_READY", serial_log)
                    qmp.command("stop")
                    copy_sparse(disks[0], disks[2])
                    with disks[2].open("rb") as image:
                        os.fsync(image.fileno())
                    qmp.command("blockdev-add", {
                        "driver": "file", "node-name": "fault-file",
                        "filename": str(disks[2]),
                    })
                    qmp.command("blockdev-add", {
                        "driver": "raw", "node-name": "fault-raw", "file": "fault-file",
                    })
                    qmp.command("blockdev-add", {
                        "driver": "blkdebug", "node-name": "fault", "image": "fault-raw",
                        "config": str(blkconf),
                    })
                    qmp.command("x-blockdev-change", {"parent": "q0", "node": "fault"})
                    qmp.command("x-blockdev-change", {"parent": "q0", "child": "children.1"})
                    qmp.command("cont")
                    serial.sendall(b"\n")
                    wait_serial(serial, b"QMP_RESULT_READY", serial_log)
                    stats = qmp.command("query-blockstats")
                    (results / "qmp-blockstats.json").write_text(
                        json.dumps(stats, indent=2) + "\n")
                    serial.sendall(b"\n")
                    wait_serial(serial, b"JBD2_TRACE_END", serial_log, timeout=60)
                    proc.wait(timeout=60)
                finally:
                    serial.close()
            finally:
                qmp.close()
                qmp_transcript.close()
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
        (results / "qemu-exit-status.txt").write_text(f"{proc.returncode}\n")
        serial_text = serial_log_path.read_text(errors="replace")
        if "UNMOUNT_RESULT rc=0" not in serial_text:
            raise RuntimeError("mainline run did not complete normal unmount")
        if f"I/O error, dev vda, sector {sector}" not in serial_text:
            raise RuntimeError("guest did not report the expected READ EIO")
        stats = json.loads((results / "qmp-blockstats.json").read_text())
        if not any(int(node.get("stats", {}).get("failed_rd_operations", 0)) > 0
                   for node in stats):
            raise RuntimeError("QEMU block stats show no failed read operation")
        if "JBD2_ABORT_HITS=0" not in serial_text:
            raise RuntimeError("journal abort probe did not show zero hits")
        stop_line = next((line for line in serial_text.splitlines()
                          if "stop: " in line and 'comm="fault-workload"' in line), "")
        if (not stop_line or "h_err=0" not in stop_line or "h_aborted=0" not in stop_line
                or "state=0" not in stop_line):
            raise RuntimeError(f"fault-workload journal-stop state missing or aborted: {stop_line}")
        tid = next((part.split("=", 1)[1] for part in stop_line.split() if part.startswith("tid=")), None)
        if tid is None or not any(f"jbd2_end_commit: dev 253,0 transaction {tid} " in line
                                  for line in serial_text.splitlines()):
            raise RuntimeError(f"unlink handle transaction {tid} has no commit tracepoint")
        handle = next((part.split("=", 1)[1] for part in stop_line.split()
                       if part.startswith("handle=")), None)
        if not handle or not any("stop_ret: " in line and f"handle={handle} ret=0" in line
                                 for line in serial_text.splitlines()):
            raise RuntimeError("fault-workload journal_stop did not return zero")

        for index in (0, 2):
            saved = results / f"disk{index}.img"
            shutil.copyfile(disks[index], saved)
            check = subprocess.run(["e2fsck", "-fn", str(saved)], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (results / f"disk{index}-e2fsck.log").write_text(check.stdout)
            save_debugfs(saved, f"stat <{parent_ino}>", results / f"disk{index}-parent-inode-{parent_ino}.txt")
            save_debugfs(saved, f"stat <{target_ino}>", results / f"disk{index}-target-inode-{target_ino}.txt")
            save_debugfs(saved, "ls -l /p", results / f"disk{index}-parent-p.txt")
            unattached = f"Unattached inode {target_ino}" in check.stdout
            entries = (results / f"disk{index}-parent-p.txt").read_text()
            name_present = re.search(
                r"^\s*" + re.escape(str(target_ino)) + r"\s+.*\sfile\s*$",
                entries, re.MULTILINE) is not None
            target = (results / f"disk{index}-target-inode-{target_ino}.txt").read_text()
            if not re.search(r"Links:\s*1\b", target):
                raise RuntimeError(f"disk{index}: target inode link count did not remain 1")
            if args.expect_fixed:
                if check.returncode != 0 or unattached or not name_present:
                    raise RuntimeError(
                        f"disk{index}: fixed run should retain /p/file and pass e2fsck -fn")
            else:
                if not unattached:
                    raise RuntimeError(
                        f"disk{index}: expected unattached inode {target_ino} not reported")
                if name_present:
                    raise RuntimeError(f"disk{index}: /p/file unexpectedly present")
        (results / "upstream-commit.txt").write_text(
            f"{UPSTREAM_SHA}  Linux 7.4-rc3 (torvalds/linux master snapshot)\n")
        print(f"mainline repro complete; results: {results}")


if __name__ == "__main__":
    main()
