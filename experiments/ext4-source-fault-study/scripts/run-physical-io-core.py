#!/usr/bin/env python3
"""Inject guest-visible blkdebug EIOs into ext4 core-count error paths."""

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
STUDY = ROOT / "experiments/ext4-source-fault-study"
RESULTS = STUDY / "results/physical-io-core-20261102"
KERNEL = ROOT / "experiments/ext4-source-fault-study/results/physical-io-20261102/unfixed-linux-6.12.1.bzImage"
BASE = pathlib.Path("/tmp/ext4-physical-io-core-base")


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


def wait_serial(sock, needle, timeout=60, transcript=None):
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
        if transcript:
            transcript.write(chunk)
            transcript.flush()
        if needle in data:
            return bytes(data)
    raise TimeoutError(f"serial did not print {needle!r}")


def qmp_add_fault(qmp, disk, sector, config, iotype, repeat):
    config.write_text(
        '[inject-error]\n'
        'event = "none"\n'
        f'iotype = "{iotype}"\n'
        f'sector = "{sector}"\n'
        'errno = "5"\n'
        f'once = "{"off" if repeat else "on"}"\n'
    )
    qmp.command("blockdev-add", {
        "driver": "file", "node-name": "fault-file", "filename": str(disk)
    })
    qmp.command("blockdev-add", {
        "driver": "raw", "node-name": "fault-raw", "file": "fault-file"
    })
    qmp.command("blockdev-add", {
        "driver": "blkdebug", "node-name": "fault", "image": "fault-raw",
        "config": str(config)
    })
    qmp.command("x-blockdev-change", {"parent": "q0", "node": "fault"})
    qmp.command("x-blockdev-change", {"parent": "q0", "child": "children.1"})


def build_initrd(out, test):
    work = pathlib.Path(tempfile.mkdtemp(prefix="ext4-physical-io-initrd-"))
    try:
        subprocess.run(
            ["bash", "-lc", f"gzip -dc {STUDY / 'build/source-fault-initramfs.cpio.gz'} | cpio -id --quiet"],
            cwd=work, check=True,
        )
        shutil.copy2(STUDY / "initramfs/physical-io-core-init", work / "init")
        os.chmod(work / "init", 0o755)
        with out.open("wb") as stream:
            proc = subprocess.Popen(
                ["bash", "-lc", "find . -print0 | cpio --null -o -H newc --quiet | gzip -1"],
                cwd=work, stdout=stream,
            )
            if proc.wait() != 0:
                raise RuntimeError("could not build initramfs")
    finally:
        shutil.rmtree(work)


def copy_sparse(src, dst):
    subprocess.run(["cp", "--sparse=always", str(src), str(dst)], check=True)


def launch(case, sector, fs_image, initrd, result_dir, iotype, repeat,
           pause_at_mutation, kernel):
    run = pathlib.Path(tempfile.mkdtemp(prefix=f"qemu-ext4-{case}-"))
    disks = [run / f"disk{i}.img" for i in range(3)]
    for disk in disks:
        copy_sparse(fs_image, disk)
    blkconf = run / "blkdebug.conf"
    qmp_path = run / "qmp.sock"
    serial_path = run / "serial.sock"
    qemu_log = result_dir / f"{case}-qemu.log"
    serial_log = result_dir / f"{case}-serial.log"
    transcript = serial_log.open("wb")
    cmd = [
        "qemu-system-x86_64", "-machine", "q35", "-m", "512M", "-smp", "2",
        "-display", "none", "-monitor", "none", "-no-reboot",
        "-kernel", str(kernel), "-initrd", str(initrd),
        "-append", f"console=ttyS0 panic=-1 loglevel=7 test={case}",
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
    (result_dir / f"{case}-qemu-command.json").write_text(json.dumps(cmd, indent=2) + "\n")
    with qemu_log.open("wb") as qlog:
        proc = subprocess.Popen(cmd, stdout=qlog, stderr=subprocess.STDOUT)
        qmp = QMP(qmp_path)
        serial_deadline = time.time() + 30
        while not serial_path.exists():
            if proc.poll() is not None or time.time() > serial_deadline:
                raise RuntimeError(f"QEMU ended before serial socket for {case}")
            time.sleep(0.05)
        serial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        serial.connect(str(serial_path))
        try:
            wait_serial(serial, b"QMP_READY", transcript=transcript)
            qmp.command("stop")
            copy_sparse(disks[0], disks[2])
            with disks[2].open("rb") as dst:
                os.fsync(dst.fileno())
            qmp.command("cont")
            if pause_at_mutation and case in {"mkdir", "rmdir", "rename"}:
                serial.sendall(b"\n")
                wait_serial(serial, b"BLOCK_TARGET_READY", timeout=30, transcript=transcript)
                serial.sendall(b"\n")
                wait_serial(serial, b"BLOCK_CACHE_DROPPED", timeout=30, transcript=transcript)
                qmp.command("stop")
                qmp_add_fault(qmp, disks[2], sector, blkconf, iotype, repeat)
                qmp.command("cont")
                serial.sendall(b"\n")
            else:
                qmp_add_fault(qmp, disks[2], sector, blkconf, iotype, repeat)
                serial.sendall(b"\n")
            wait_serial(serial, b"QMP_RESULT_READY", timeout=90, transcript=transcript)
            serial.sendall(b"\n")
            wait_serial(serial, b"UNMOUNT_RESULT rc=0", timeout=60, transcript=transcript)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=10)
                raise RuntimeError(f"QEMU did not power down for {case}")
        finally:
            serial.close()
            qmp.close()
            transcript.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
    (result_dir / f"{case}-qemu-exit-status.txt").write_text(f"{proc.returncode}\n")
    return disks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=["mkdir", "rmdir", "rename", "group-extend", "group-add"])
    parser.add_argument("image", type=pathlib.Path)
    parser.add_argument("sector", type=int, help="512-byte sector to fail on a real guest READ bio")
    parser.add_argument("initrd", type=pathlib.Path)
    parser.add_argument("--iotype", choices=["read", "write", "write-zeroes"], default="read")
    parser.add_argument("--repeat", action="store_true", help="fail every matching I/O")
    parser.add_argument("--kernel", type=pathlib.Path, default=KERNEL,
                        help="kernel bzImage (defaults to the recorded 6.12.1 test build)")
    parser.add_argument("--pause-at-mutation", action="store_true",
                        help="arm blkdebug after ext4 pauses at the mutation boundary")
    parser.add_argument("--results", type=pathlib.Path, default=RESULTS)
    args = parser.parse_args()
    args.results.mkdir(parents=True, exist_ok=True)
    disks = launch(args.case, args.sector, args.image, args.initrd, args.results,
                   args.iotype, args.repeat, args.pause_at_mutation, args.kernel)
    for index, disk in enumerate(disks):
        if not disk.exists():
            continue
        shutil.copyfile(disk, args.results / f"{args.case}-disk{index}.img")
        fsck = subprocess.run(["e2fsck", "-fn", str(args.results / f"{args.case}-disk{index}.img")],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (args.results / f"{args.case}-disk{index}-e2fsck.log").write_text(fsck.stdout)
        print(f"{args.case} disk{index}: e2fsck_rc={fsck.returncode}")
    if disks:
        shutil.rmtree(disks[0].parent)
    print(f"results={args.results}")


if __name__ == "__main__":
    main()
