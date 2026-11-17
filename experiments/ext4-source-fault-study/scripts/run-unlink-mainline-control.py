#!/usr/bin/env python3
"""Run an unmodified-mainline no-fault unlink control for the READ-EIO repro."""

import argparse
import importlib.util
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
RESULTS_DEFAULT = ROOT / "experiments/ext4-source-fault-study/results/unlink-mainline-fresh-clean-20261116"
REPRO = ROOT / "experiments/ext4-source-fault-study/scripts/run-unlink-mainline.py"
spec = importlib.util.spec_from_file_location("unlink_mainline_repro", REPRO)
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=pathlib.Path)
    parser.add_argument("--kernel", type=pathlib.Path, default=repro.KERNEL)
    parser.add_argument("--init", type=pathlib.Path)
    parser.add_argument("--results", type=pathlib.Path, default=RESULTS_DEFAULT)
    args = parser.parse_args()
    image = args.image or args.results / "seed-mainline.img"
    init = args.init or repro.INIT
    results = args.results
    results.mkdir(parents=True, exist_ok=True)
    for required in (image, init, args.kernel):
        if not required.is_file():
            raise FileNotFoundError(required)
    initrd = results / "control-initramfs.cpio.gz"
    repro.build_initrd(initrd, init)

    with tempfile.TemporaryDirectory(prefix="qemu-unlink-mainline-control-") as temp:
        run = pathlib.Path(temp)
        disk = run / "disk.img"
        repro.copy_sparse(image, disk)
        serial_path = run / "serial.sock"
        command = [
            "qemu-system-x86_64", "-machine", "q35", "-m", "512M", "-smp", "2",
            "-display", "none", "-monitor", "none", "-no-reboot",
            "-kernel", str(args.kernel), "-initrd", str(initrd),
            "-append", "console=ttyS0 panic=-1 loglevel=7",
            "-chardev", f"socket,id=serial0,path={serial_path},server=on,wait=off",
            "-serial", "chardev:serial0",
            "-drive", f"file={disk},if=virtio,format=raw",
        ]
        (results / "control-qemu-command.json").write_text(json.dumps(command, indent=2) + "\n")
        with (results / "control-qemu.log").open("wb") as qemu_log, \
             (results / "control-serial.log").open("wb") as serial_log:
            proc = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT)
            deadline = time.time() + 30
            while not serial_path.exists():
                if proc.poll() is not None or time.time() >= deadline:
                    raise RuntimeError("QEMU ended before serial socket appeared")
                time.sleep(0.05)
            serial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            serial.connect(str(serial_path))
            try:
                wait_serial(serial, b"QMP_READY", serial_log)
                serial.sendall(b"\n")
                wait_serial(serial, b"QMP_RESULT_READY", serial_log)
                serial.sendall(b"\n")
                wait_serial(serial, b"JBD2_TRACE_END", serial_log, timeout=60)
                proc.wait(timeout=60)
            finally:
                serial.close()
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
        (results / "control-qemu-exit-status.txt").write_text(f"{proc.returncode}\n")
        if proc.returncode != 0:
            raise RuntimeError(f"control QEMU exited with status {proc.returncode}")
        serial_text = (results / "control-serial.log").read_text(errors="replace")
        if "WORKLOAD_RESULT op=unlink rc=0 errno=0" not in serial_text:
            raise RuntimeError("control unlink did not succeed")
        if "UNMOUNT_RESULT rc=0" not in serial_text or "I/O error" in serial_text:
            raise RuntimeError("control had an I/O error or failed normal unmount")

        saved = results / "control-disk.img"
        shutil.copy2(disk, saved)
        check = subprocess.run(["e2fsck", "-fn", str(saved)], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (results / "control-e2fsck.log").write_text(check.stdout)
        repro.save_debugfs(saved, "ls -l /p", results / "control-parent-p.txt")
        resolved = (results / "resolved-input.txt").read_text().splitlines()
        target_ino = int(next(line.split("=", 1)[1] for line in resolved
                              if line.startswith("target_inode=")))
        repro.save_debugfs(saved, f"stat <{target_ino}>",
                           results / f"control-target-inode-{target_ino}.txt")
        if check.returncode != 0:
            raise RuntimeError(f"control e2fsck -fn returned {check.returncode}")
        if f"Links: 0" not in (results / f"control-target-inode-{target_ino}.txt").read_text():
            raise RuntimeError("control target inode link count did not reach zero")
        print(f"no-fault control passed; results: {results}")


if __name__ == "__main__":
    main()
