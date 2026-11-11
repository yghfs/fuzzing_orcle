#!/usr/bin/env python3
"""Replay one retained candidate workload with a guest-visible blkdebug EIO."""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
KERNEL = ROOT / "experiments/ext4-source-fault-study/results/physical-io-20261102/unfixed-linux-6.12.1.bzImage"
GUEST_INIT = ROOT / "experiments/ext4-counter-trace/tests/guest-realio-replay.sh"


class QMP:
    def __init__(self, path: pathlib.Path):
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


def wait_serial(sock: socket.socket, needle: bytes, log, timeout=90):
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
        log.write(chunk)
        log.flush()
        if needle in data:
            return bytes(data)
    raise TimeoutError(f"serial did not print {needle!r}")


def sparse_copy(source: pathlib.Path, target: pathlib.Path):
    subprocess.run(["cp", "--sparse=always", str(source), str(target)], check=True)


def prepare_initrd(source: pathlib.Path, workload: pathlib.Path,
                   run: pathlib.Path, archive: pathlib.Path) -> pathlib.Path:
    """Build a private initrd containing exactly the requested workload."""
    root = run / "initrd-root"
    root.mkdir()
    with source.open("rb") as source_stream:
        magic = source_stream.read(2)
    if magic == b"\x1f\x8b":
        decompressed = subprocess.Popen(["gzip", "-dc", str(source)],
                                        stdout=subprocess.PIPE)
        try:
            subprocess.run(["cpio", "-id", "--quiet"], cwd=root,
                           stdin=decompressed.stdout, check=True)
        finally:
            assert decompressed.stdout is not None
            decompressed.stdout.close()
            if decompressed.wait() != 0:
                raise RuntimeError("gzip failed while extracting initrd")
    else:
        with source.open("rb") as source_stream:
            subprocess.run(["cpio", "-id", "--quiet"], cwd=root,
                           stdin=source_stream, check=True)

    target = root / "bin/replay-workload"
    target.parent.mkdir(parents=True, exist_ok=True)
    if workload.suffix == ".c":
        subprocess.run(["gcc", "-static", "-O2", "-pthread", "-o",
                        str(target), str(workload)], check=True)
    else:
        shutil.copy2(workload, target)
    target.chmod(0o755)
    shutil.copy2(GUEST_INIT, root / "init")
    (root / "init").chmod(0o755)

    find = subprocess.Popen(["find", ".", "-print0"], cwd=root,
                            stdout=subprocess.PIPE)
    try:
        cpio = subprocess.Popen(["cpio", "--null", "-o", "-H", "newc"],
                                cwd=root, stdin=find.stdout,
                                stdout=subprocess.PIPE)
        assert cpio.stdout is not None
        with archive.open("wb") as output:
            gzip = subprocess.run(["gzip", "-9"], stdin=cpio.stdout,
                                  stdout=output)
        cpio.stdout.close()
        if cpio.wait() != 0 or gzip.returncode != 0:
            raise RuntimeError("failed to build candidate replay initrd")
    finally:
        assert find.stdout is not None
        find.stdout.close()
        if find.wait() != 0:
            raise RuntimeError("find failed while building candidate replay initrd")
    return archive


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    run = pathlib.Path(tempfile.mkdtemp(prefix="ext4-realio-candidate-"))
    disks = [run / f"disk{i}.img" for i in range(3)]
    for disk in disks:
        sparse_copy(args.image, disk)
    qmp_path = run / "qmp.sock"
    serial_path = run / "serial.sock"
    config = run / "blkdebug.conf"
    if not args.no_fault:
        config.write_text(
            '[inject-error]\n'
            'event = "none"\n'
            f'iotype = "{args.iotype}"\n'
            f'sector = "{args.sector}"\n'
            'errno = "5"\n'
            'once = "on"\n'
        )
    effective_initrd = prepare_initrd(
        args.initrd, args.workload, run, args.output / "realio-initrd.cpio.gz")
    command = [
        "qemu-system-x86_64", "-machine", "q35", "-m", "512M", "-smp", "2",
        "-display", "none", "-monitor", "none", "-no-reboot",
        "-kernel", str(args.kernel), "-initrd", str(effective_initrd),
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
    (args.output / "qemu-command.json").write_text(json.dumps(command, indent=2) + "\n")
    if not args.no_fault:
        (args.output / "blkdebug.conf").write_text(config.read_text())
    (args.output / "workload-source.txt").write_text(str(args.workload) + "\n")
    (args.output / "effective-initrd.txt").write_text(str(effective_initrd) + "\n")
    qemu_log = (args.output / "qemu.log").open("wb")
    serial_log = (args.output / "serial.log").open("wb")
    proc = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT)
    qmp = QMP(qmp_path)
    serial = None
    try:
        deadline = time.time() + 30
        while not serial_path.exists():
            if proc.poll() is not None or time.time() >= deadline:
                raise RuntimeError("QEMU ended before serial socket")
            time.sleep(0.05)
        serial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        serial.connect(str(serial_path))
        wait_serial(serial, b"REALIO_REPLAY_READY", serial_log)
        if not args.no_fault:
            qmp.command("stop")
            sparse_copy(disks[0], disks[2])
            qmp.command("blockdev-add", {"driver": "file", "node-name": "fault-file", "filename": str(disks[2])})
            qmp.command("blockdev-add", {"driver": "raw", "node-name": "fault-raw", "file": "fault-file"})
            qmp.command("blockdev-add", {"driver": "blkdebug", "node-name": "fault", "image": "fault-raw", "config": str(config)})
            qmp.command("x-blockdev-change", {"parent": "q0", "node": "fault"})
            qmp.command("x-blockdev-change", {"parent": "q0", "child": "children.1"})
            qmp.command("cont")
        graph = {
            "nodes": qmp.command("query-named-block-nodes"),
            "blocks": qmp.command("query-block"),
        }
        (args.output / "qmp-block-graph.json").write_text(
            json.dumps(graph, indent=2) + "\n")
        serial.sendall(b"\n")
        wait_serial(serial, b"REALIO_UNMOUNT_EXIT", serial_log, timeout=120)
        proc.wait(timeout=30)
    finally:
        if serial is not None:
            serial.close()
        qmp.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        qemu_log.close()
        serial_log.close()
    (args.output / "qemu-exit-status.txt").write_text(f"{proc.returncode}\n")
    for index, disk in enumerate(disks):
        saved = args.output / f"disk{index}.img"
        shutil.copyfile(disk, saved)
        check = subprocess.run(["e2fsck", "-fn", str(saved)], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (args.output / f"disk{index}-e2fsck.txt").write_text(check.stdout)
    shutil.rmtree(run)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=pathlib.Path, required=True)
    parser.add_argument("--workload", type=pathlib.Path, required=True)
    parser.add_argument("--initrd", type=pathlib.Path, required=True)
    parser.add_argument("--sector", type=int, required=True)
    parser.add_argument("--iotype", choices=("read", "write", "flush"), required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--kernel", type=pathlib.Path, default=KERNEL)
    parser.add_argument("--no-fault", action="store_true",
                        help="run the same guest workload without blkdebug")
    args = parser.parse_args()
    if not args.workload.is_file():
        raise SystemExit(f"missing workload: {args.workload}")
    if not args.initrd.is_file():
        raise SystemExit(f"missing initrd: {args.initrd}")
    if not GUEST_INIT.is_file():
        raise SystemExit(f"missing guest init script: {GUEST_INIT}")
    run(args)


if __name__ == "__main__":
    main()
