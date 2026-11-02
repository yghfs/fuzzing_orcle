#!/usr/bin/env python3
"""Repeat the physical unlink EIO case while tracing JBD2 calls/state."""

import json
import hashlib
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
STUDY = ROOT / "experiments/ext4-source-fault-study"
RESULTS = STUDY / "results/unlink-jbd2-state-20261102"
SOURCE_INITRD = STUDY / "results/physical-io-20261102/physical-io-initramfs.cpio.gz"
KERNEL = STUDY / "results/physical-io-20261102/unfixed-linux-6.12.1.bzImage"
IMAGE = pathlib.Path("/tmp/ext4-physical-io-clean-template-20261102.img")
SECTOR = 2312
IMAGE_SHA256 = "b00d323b78d6430f842de6d3a75c99c0ab4305ca9f7d2aa47476e6aa3f6bf933"


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


def build_initrd(out):
    with tempfile.TemporaryDirectory(prefix="unlink-jbd2-initrd-") as temp:
        work = pathlib.Path(temp)
        source = subprocess.Popen(["gzip", "-dc", str(SOURCE_INITRD)], stdout=subprocess.PIPE)
        subprocess.run(["cpio", "-id", "--quiet"], cwd=work,
                       stdin=source.stdout, check=True)
        if source.wait() != 0:
            raise RuntimeError("could not unpack source initramfs")
        init = STUDY / "initramfs/unlink-jbd2-trace-init"
        shutil.copy2(init, work / "init")
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
                raise RuntimeError("could not build trace initramfs")


def copy_sparse(source, target):
    subprocess.run(["cp", "--sparse=always", str(source), str(target)], check=True)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    if not IMAGE.is_file():
        raise FileNotFoundError(f"clean physical-I/O template is missing: {IMAGE}")
    digest = hashlib.sha256(IMAGE.read_bytes()).hexdigest()
    if digest != IMAGE_SHA256:
        raise RuntimeError(f"unexpected clean template checksum: {digest}")
    (RESULTS / "unlink-jbd2-input-template.sha256").write_text(
        f"{digest}  {IMAGE}\n")
    if not KERNEL.is_file():
        raise FileNotFoundError(f"unfixed test kernel is missing: {KERNEL}")

    initrd = RESULTS / "unlink-jbd2-trace-initramfs.cpio.gz"
    build_initrd(initrd)

    with tempfile.TemporaryDirectory(prefix="qemu-unlink-jbd2-state-") as temp:
        run = pathlib.Path(temp)
        disks = [run / f"disk{i}.img" for i in range(3)]
        copy_sparse(IMAGE, disks[0])
        copy_sparse(IMAGE, disks[1])
        blkconf = run / "parent-sector-2312.blkdebug.conf"
        qmp_path = run / "qmp.sock"
        serial_path = run / "serial.sock"
        blkconf.write_text(
            '[inject-error]\n'
            'event = "none"\n'
            'iotype = "read"\n'
            f'sector = "{SECTOR}"\n'
            'errno = "5"\n'
            'once = "on"\n'
        )
        shutil.copy2(blkconf, RESULTS / blkconf.name)

        command = [
            "qemu-system-x86_64", "-machine", "q35", "-m", "512M", "-smp", "2",
            "-display", "none", "-monitor", "none", "-no-reboot",
            "-kernel", str(KERNEL), "-initrd", str(initrd),
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
        (RESULTS / "unlink-jbd2-qemu-command.json").write_text(
            json.dumps(command, indent=2) + "\n")
        serial_path_log = RESULTS / "unlink-jbd2-serial.log"
        qmp_transcript = (RESULTS / "unlink-jbd2-qmp-transcript.jsonl").open("w")
        qemu_log = (RESULTS / "unlink-jbd2-qemu.log").open("wb")
        serial_log = serial_path_log.open("wb")
        with qemu_log, serial_log:
            proc = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT)
            qmp = QMP(qmp_path, qmp_transcript)
            try:
                deadline = time.time() + 30
                while not serial_path.exists():
                    if proc.poll() is not None or time.time() >= deadline:
                        raise RuntimeError("QEMU ended before creating serial socket")
                    time.sleep(0.05)
                serial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                serial.connect(str(serial_path))
                try:
                    wait_serial(serial, b"QMP_READY", serial_log)
                    qmp.command("stop")
                    copy_sparse(disks[0], disks[2])
                    with disks[2].open("rb") as disk:
                        os.fsync(disk.fileno())
                    qmp.command("blockdev-add", {
                        "driver": "file", "node-name": "file2", "filename": str(disks[2])
                    })
                    qmp.command("blockdev-add", {
                        "driver": "raw", "node-name": "raw2", "file": "file2"
                    })
                    qmp.command("blockdev-add", {
                        "driver": "blkdebug", "node-name": "fault2",
                        "image": "raw2", "config": str(blkconf)
                    })
                    qmp.command("x-blockdev-change", {"parent": "q0", "node": "fault2"})
                    qmp.command("x-blockdev-change", {"parent": "q0", "child": "children.1"})
                    qmp.command("cont")
                    serial.sendall(b"\n")
                    wait_serial(serial, b"QMP_RESULT_READY", serial_log)
                    blockstats = qmp.command("query-blockstats")
                    (RESULTS / "unlink-jbd2-qmp-blockstats.json").write_text(
                        json.dumps(blockstats, indent=2) + "\n")
                    serial.sendall(b"\n")
                    wait_serial(serial, b"JBD2_TRACE_END", serial_log, timeout=60)
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        proc.wait(timeout=10)
                        raise RuntimeError("QEMU did not power down")
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
        (RESULTS / "unlink-jbd2-qemu-exit-status.txt").write_text(f"{proc.returncode}\n")
        serial_text = serial_path_log.read_text(errors="replace")
        if "UNMOUNT_RESULT rc=0" not in serial_text:
            raise RuntimeError("unlink trace run did not complete a normal unmount")

        for index in (0, 2):
            saved = RESULTS / f"unlink-jbd2-disk{index}.img"
            shutil.copyfile(disks[index], saved)
            fsck = subprocess.run(["e2fsck", "-fn", str(saved)], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (RESULTS / f"unlink-jbd2-disk{index}-e2fsck.log").write_text(fsck.stdout)
            stat = subprocess.run(["debugfs", "-R", "stat <2064>", str(saved)], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (RESULTS / f"unlink-jbd2-disk{index}-inode-2064.txt").write_text(stat.stdout)
            ls = subprocess.run(["debugfs", "-R", "ls -l /p", str(saved)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (RESULTS / f"unlink-jbd2-disk{index}-parent-p.txt").write_text(ls.stdout)
            print(f"disk{index}: e2fsck_rc={fsck.returncode}")


if __name__ == "__main__":
    main()
