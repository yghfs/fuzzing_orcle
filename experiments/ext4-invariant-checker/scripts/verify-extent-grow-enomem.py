#!/usr/bin/env python3
"""Build and compare upstream 6.12.1 before/after the extent-grow ENOMEM fix.

Only an isolated source copy gets the test-only NULL-return hook. No checker
or natural memory pressure is used: the hook exercises the caller's real
error branch, not an actual failed memory allocation inside sb_getblk_gfp().
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[3]
STUDY = ROOT / "experiments/ext4-invariant-checker"
FIX = STUDY / "patches/0002-ext4-free-unreachable-extent-block-on-enomem.patch"
GETBLK = "\tbh = sb_getblk_gfp(inode->i_sb, newblock, __GFP_MOVABLE | GFP_NOFS);"
HOOK = """\tif (unlikely(extent_grow_test_failures &&
\t\t     !strcmp(inode->i_sb->s_id, "vdb"))) {
\t\textent_grow_test_failures--;
\t\tpr_info("extent-grow-test: ino=%lu newblock=%llu blocks=%llu remaining=%u\\n",
\t\t\tinode->i_ino, (unsigned long long)newblock,
\t\t\t(unsigned long long)inode->i_blocks, extent_grow_test_failures);
\t\tbh = NULL;
\t} else {
\t\tbh = sb_getblk_gfp(inode->i_sb, newblock, __GFP_MOVABLE | GFP_NOFS);
\t}"""


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def write_private(path, text):
    # The source copy uses hard links. Replace, never overwrite, a shared inode.
    temp = path.with_name(path.name + ".validation-tmp")
    temp.write_text(text)
    temp.replace(path)


def build(source, output, evidence, label, jobs):
    print(f"Building {label} kernel", flush=True)
    command = ["make", "-C", source, f"O={output}", "ARCH=x86_64",
               "CC=gcc-14", "HOSTCC=gcc-14", f"-j{jobs}", "bzImage"]
    with (evidence / f"{label}-build.log").open("w") as log:
        run(command, stdout=log, stderr=subprocess.STDOUT)
    image = evidence / f"{label}.bzImage"
    shutil.copyfile(output / "arch/x86/boot/bzImage", image)
    return image


def check_vm(evidence, image, base_root, pristine, workload, label,
             stage=1, expected_errno=12):
    case = evidence / label
    case.mkdir()
    disk = case / "test.raw"
    run(["cp", "--sparse=always", pristine, disk])
    overlay = case / "root.qcow2"
    run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw", "-b", base_root, overlay])
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    opts = ["-i", ROOT / "work/debian.id_rsa", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    ssh = ["ssh", *opts, "-p", str(port), "root@127.0.0.1"]
    cmd = ["qemu-system-x86_64", "-machine", "q35,accel=tcg", "-cpu", "max",
           "-m", "768M", "-smp", "2", "-kernel", image,
           "-drive", f"file={overlay},format=qcow2,if=virtio",
           "-drive", f"file={disk},format=raw,if=virtio",
           "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22",
           "-device", "virtio-net-pci,netdev=n0",
           "-append", "root=/dev/vda rw console=ttyS0 net.ifnames=0 panic=-1",
           "-display", "none", "-monitor", "none", "-serial", f"file:{case}/serial.log"]
    (case / "qemu-command.txt").write_text(shlex.join([str(x) for x in cmd]) + "\n")
    print(f"Running {label} VM", flush=True)
    with (case / "qemu.log").open("w") as log:
        vm = subprocess.Popen([str(x) for x in cmd], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if vm.poll() is not None:
                    raise RuntimeError(f"{label} QEMU exited during boot")
                ready = subprocess.run([str(x) for x in ssh] + ["true"],
                                       capture_output=True, timeout=8)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"{label} SSH boot timeout")
            run(["scp", *opts, "-P", str(port), workload,
                 "root@127.0.0.1:/tmp/extent-grow-regression"], capture_output=True, timeout=30)
            parameter = "/sys/module/ext4/parameters/extent_grow_test_failures"
            script = f"""set -eu
mkdir -p /mnt/extent-grow
mount /dev/vdb /mnt/extent-grow
test ! -e /sys/kernel/debug/ext4/vdb/invariant_summary
if test -e /sys/module/ext4/parameters/extent_grow_test_stage; then
  echo {stage} > /sys/module/ext4/parameters/extent_grow_test_stage
fi
echo 7 > {parameter}
set +e
/tmp/extent-grow-regression /mnt/extent-grow fault 7 {expected_errno}
rc=$?
set -e
echo fault_workload_rc=$rc
test "$(cat {parameter})" = 0
"""
            if label == "fixed":
                script += f"""test "$rc" = 0
echo 1 > {parameter}
/tmp/extent-grow-regression /mnt/extent-grow retry 1 {expected_errno}
test "$(cat {parameter})" = 0
/tmp/extent-grow-regression /mnt/extent-grow success 1
"""
            else:
                script += 'test "$rc" = 1\n'
            script += """sync
umount /mnt/extent-grow
echo clean_unmount=1
dmesg | grep extent-grow-test
"""
            (case / "guest.sh").write_text(script)
            result = subprocess.run([str(x) for x in ssh] + [script],
                                    capture_output=True, timeout=120)
            (case / "runtime.txt").write_bytes(result.stdout + result.stderr)
            result.check_returncode()
            subprocess.run([str(x) for x in ssh] + ["systemctl poweroff"],
                           capture_output=True, timeout=15, check=False)
            vm.wait(timeout=60)
            if vm.returncode != 0:
                raise RuntimeError(f"{label} QEMU exited with {vm.returncode}")
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:
                    vm.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vm.kill()
                    vm.wait()
    runtime = (case / "runtime.txt").read_text()
    hooks = re.findall(r"extent-grow-test: ino=(\d+) newblock=(\d+)", runtime)
    expected_hooks = 8 if label == "fixed" else 7
    assert len(hooks) == expected_hooks, (label, hooks)
    assert "clean_unmount=1" in runtime
    fsck = subprocess.run(["e2fsck", "-fn", str(disk)], capture_output=True,
                          env={**os.environ, "LC_ALL": "C"})
    fsck_text = (fsck.stdout + fsck.stderr).decode()
    (case / "fsck.txt").write_text(fsck_text)
    expected_rc = 0 if label == "fixed" else 4
    assert fsck.returncode == expected_rc, (label, fsck.returncode, fsck_text)
    if label == "baseline":
        assert len(re.findall(r"i_blocks is 40, should be 32", fsck_text)) == 7
        assert "Block bitmap differences:" in fsck_text
    else:
        assert "Block bitmap differences:" not in fsck_text
        assert "i_blocks is" not in fsck_text
    with (case / "debugfs.txt").open("w") as log:
        for path in [f"fault-{i}" for i in range(7)] + (["retry-0", "success-0"] if label == "fixed" else []):
            run(["debugfs", "-R", f"stat /{path}", disk], stdout=log, stderr=subprocess.STDOUT)
        if label == "baseline":
            blocks = " ".join(block for _, block in hooks)
            run(["debugfs", "-R", f"icheck {blocks}", disk], stdout=log, stderr=subprocess.STDOUT)
            for _, block in hooks:
                run(["debugfs", "-R", f"testb {block}", disk], stdout=log, stderr=subprocess.STDOUT)
    return {"injected_failures": len(hooks), "fsck_rc": fsck.returncode,
            "clean_unmount": True, "fault_files": 7}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    evidence = (args.out or Path(tempfile.mkdtemp(prefix="extent-grow-enomem-", dir=ROOT / "work"))).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    source = evidence / "source"
    output = evidence / "build"
    # Use the upstream source, rather than any previously instrumented checker tree.
    upstream = ROOT / "experiments/ext4-counter-trace/kernel/linux-6.12.1"
    print(f"Evidence: {evidence}", flush=True)
    run(["cp", "-al", upstream, source])
    extents = source / "fs/ext4/extents.c"
    original = extents.read_text()
    assert "ext4_checker_" not in original
    text = original.replace("#include <linux/fs.h>", "#include <linux/fs.h>\n#include <linux/moduleparam.h>", 1)
    declarations = ("static unsigned int extent_grow_test_failures;\n"
                    "module_param_named(extent_grow_test_failures, extent_grow_test_failures, uint, 0600);\n\n")
    text = text.replace("/*\n * used by extent splitting.", declarations + "/*\n * used by extent splitting.", 1)
    grow_start = text.index("static int ext4_ext_grow_indepth(")
    grow_end = text.index("\n/*\n * ext4_ext_create_new_leaf:", grow_start)
    grow = text[grow_start:grow_end]
    assert grow.count(GETBLK) == 1
    text = text[:grow_start] + grow.replace(GETBLK, HOOK) + text[grow_end:]
    write_private(extents, text)
    (evidence / "baseline-extents.c").write_text(text)
    (evidence / "test-hook.diff").write_text(subprocess.run(
        ["diff", "-u", "--label", "a/fs/ext4/extents.c", "--label", "b/fs/ext4/extents.c",
         upstream / "fs/ext4/extents.c", extents], capture_output=True, text=True).stdout)
    output.mkdir()
    shutil.copyfile(STUDY / "build/linux-invariant-checker/.config", output / ".config")
    run([source / "scripts/config", "--file", output / ".config",
         "--disable", "KASAN", "--disable", "KCOV", "--disable", "DEBUG_INFO",
         "--disable", "DEBUG_INFO_DWARF5", "--disable", "DEBUG_INFO_DWARF4",
         "--disable", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
         "--enable", "DEBUG_INFO_NONE", "--disable", "LOCALVERSION_AUTO",
         "--disable", "EXT4_INVARIANT_CHECKER", "--disable", "EXT4_FAULT_INJECTION"])
    run(["make", "-s", "-C", source, f"O={output}", "ARCH=x86_64",
         "CC=gcc-14", "HOSTCC=gcc-14", "olddefconfig"], stdout=subprocess.DEVNULL)
    baseline = build(source, output, evidence, "baseline", args.jobs)
    # Temporarily remove the hook so we can apply the exact production patch.
    write_private(extents, text.replace(HOOK, GETBLK, 1))
    run(["patch", "--batch", "--fuzz=0", "-p1", "-d", source],
        input=FIX.read_bytes(), capture_output=True)
    fixed_text = extents.read_text()
    grow_end = fixed_text.index("\n/*\n * ext4_ext_create_new_leaf:", grow_start)
    grow = fixed_text[grow_start:grow_end]
    fixed_text = fixed_text[:grow_start] + grow.replace(GETBLK, HOOK, 1) + fixed_text[grow_end:]
    write_private(extents, fixed_text)
    (evidence / "fixed-extents.c").write_text(fixed_text)
    fixed = build(source, output, evidence, "fixed", args.jobs)
    shutil.copyfile(output / ".config", evidence / "kernel.config")
    shutil.copyfile(FIX, evidence / FIX.name)
    workload = evidence / "workload"
    shutil.copyfile(ROOT / "tests/ext4-extent-grow-enomem-regression.c", evidence / "workload.c")
    run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-o", workload,
         evidence / "workload.c"])
    pristine = evidence / "pristine.raw"
    run(["qemu-img", "create", "-q", "-f", "raw", pristine, "256M"])
    run(["mke2fs", "-q", "-F", "-t", "ext4", "-O", "^orphan_file", "-b", "4096",
         "-N", "8192", "-J", "size=4", "-E", "lazy_itable_init=0", pristine])
    with (evidence / "pristine-fsck.txt").open("w") as log:
        run(["e2fsck", "-fn", pristine], stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "LC_ALL": "C"})
    base_root = evidence / "root-base.raw"
    run(["cp", "--sparse=always", ROOT / "work/debian-clean-smalljournal.img", base_root])
    run(["tune2fs", "-O", "^orphan_file", base_root], stdout=subprocess.DEVNULL)
    summary = {}
    for label, image in [("baseline", baseline), ("fixed", fixed)]:
        summary[label] = check_vm(evidence, image, base_root, pristine, workload, label)
    files = [baseline, fixed, evidence / "kernel.config", workload, FIX]
    (evidence / "sha256.txt").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n" for path in files))
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    print(f"PASS: {evidence}", flush=True)


if __name__ == "__main__":
    main()
