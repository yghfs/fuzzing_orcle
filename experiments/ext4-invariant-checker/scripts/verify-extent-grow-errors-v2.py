#!/usr/bin/env python3
"""Compare all three unpublished extent-root error paths on Linux 6.12.1.

The isolated test kernels replace one helper result at its caller boundary.
This does not simulate a real aborted journal or helper-internal side effects.
"""
import argparse
import errno
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[3]
STUDY = ROOT / "experiments/ext4-invariant-checker"
SPEC = importlib.util.spec_from_file_location(
    "extent_grow_vm", Path(__file__).with_name("verify-extent-grow-enomem.py"))
VM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VM)
DECLARATIONS = """static unsigned int extent_grow_test_failures;
module_param_named(extent_grow_test_failures, extent_grow_test_failures, uint, 0600);
static unsigned int extent_grow_test_stage = 1;
module_param_named(extent_grow_test_stage, extent_grow_test_stage, uint, 0600);

static bool extent_grow_test_fail(struct inode *inode, ext4_fsblk_t block,
				 unsigned int stage)
{
	if (!extent_grow_test_failures || extent_grow_test_stage != stage ||
	    strcmp(inode->i_sb->s_id, "vdb"))
		return false;
	extent_grow_test_failures--;
	pr_info("extent-grow-test: ino=%lu newblock=%llu stage=%u remaining=%u\\n",
		inode->i_ino, (unsigned long long)block, stage,
		extent_grow_test_failures);
	return true;
}

"""


def instrument(text):
    assert "extent_grow_test_fail" not in text
    text = text.replace("#include <linux/fs.h>",
                        "#include <linux/fs.h>\n#include <linux/moduleparam.h>", 1)
    text = text.replace("/*\n * used by extent splitting.",
                        DECLARATIONS + "/*\n * used by extent splitting.", 1)
    start = text.index("static int ext4_ext_grow_indepth(")
    end = text.index("\n/*\n * ext4_ext_create_new_leaf:", start)
    grow = text[start:end]
    operations = [
        (VM.GETBLK, "\tif (extent_grow_test_fail(inode, newblock, 1))\n"
         "\t\tbh = NULL;\n\telse\n\t" + VM.GETBLK),
        ("\terr = ext4_journal_get_create_access(handle, inode->i_sb, bh,\n"
         "\t\t\t\t\t     EXT4_JTR_NONE);",
         "\tif (extent_grow_test_fail(inode, newblock, 2))\n"
         "\t\terr = -EIO;\n\telse\n"
         "\t\terr = ext4_journal_get_create_access(handle, inode->i_sb, bh,\n"
         "\t\t\t\t\t\t     EXT4_JTR_NONE);"),
        ("\terr = ext4_handle_dirty_metadata(handle, inode, bh);",
         "\tif (extent_grow_test_fail(inode, newblock, 3))\n"
         "\t\terr = -EIO;\n\telse\n"
         "\t\terr = ext4_handle_dirty_metadata(handle, inode, bh);"),
    ]
    for original, replacement in operations:
        assert grow.count(original) == 1, original
        grow = grow.replace(original, replacement, 1)
    return text[:start] + grow + text[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--reuse-build", type=Path)
    args = parser.parse_args()
    evidence = args.out.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    patch = args.patch.resolve()
    upstream = ROOT / "experiments/ext4-counter-trace/kernel/linux-6.12.1"
    source = evidence / "source"
    VM.run(["cp", "-al", upstream, source])
    extents = source / "fs/ext4/extents.c"
    original = extents.read_text()
    assert "ext4_checker_" not in original
    baseline = instrument(original)
    (evidence / "baseline-extents.c").write_text(baseline)
    hook = subprocess.run(["diff", "-u", "--label", "a/fs/ext4/extents.c",
                           "--label", "b/fs/ext4/extents.c", str(extents), "-"],
                          input=baseline, text=True, capture_output=True)
    assert hook.returncode == 1
    (evidence / "test-hook.diff").write_text(hook.stdout)
    VM.write_private(extents, baseline)
    output = evidence / "build"
    if args.reuse_build:
        VM.run(["cp", "-a", "--reflink=auto", args.reuse_build.resolve(), output])
    else:
        output.mkdir()
        shutil.copyfile(STUDY / "build/linux-invariant-checker/.config", output / ".config")
    VM.run([source / "scripts/config", "--file", output / ".config",
            "--disable", "KASAN", "--disable", "KCOV", "--disable", "DEBUG_INFO",
            "--disable", "DEBUG_INFO_DWARF5", "--disable", "DEBUG_INFO_DWARF4",
            "--disable", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
            "--enable", "DEBUG_INFO_NONE", "--disable", "LOCALVERSION_AUTO",
            "--disable", "EXT4_INVARIANT_CHECKER", "--disable", "EXT4_FAULT_INJECTION"])
    VM.run(["make", "-s", "-C", source, f"O={output}", "ARCH=x86_64",
            "CC=gcc-14", "HOSTCC=gcc-14", "olddefconfig"])
    before = VM.build(source, output, evidence, "baseline", args.jobs)
    VM.write_private(extents, original)
    applied = VM.run(["patch", "--batch", "--fuzz=0", "-p1", "-d", source],
                     input=patch.read_bytes(), capture_output=True)
    production = extents.read_text()
    (evidence / "fixed-production-extents.c").write_text(production)
    fixed = instrument(production)
    (evidence / "fixed-extents.c").write_text(fixed)
    VM.write_private(extents, fixed)
    (evidence / "patch-equivalence.txt").write_text(
        "The fixed test source is exactly instrument(production source after applying "
        "the exported patch); no other functional changes.\n" + applied.stdout.decode())
    after = VM.build(source, output, evidence, "fixed", args.jobs)
    shutil.copyfile(output / ".config", evidence / "kernel.config")
    shutil.copyfile(patch, evidence / patch.name)
    workload = evidence / "workload"
    shutil.copyfile(ROOT / "tests/ext4-extent-grow-enomem-regression.c", evidence / "workload.c")
    VM.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-o", workload,
            evidence / "workload.c"])
    pristine = evidence / "pristine.raw"
    VM.run(["qemu-img", "create", "-q", "-f", "raw", pristine, "256M"])
    VM.run(["mke2fs", "-q", "-F", "-t", "ext4", "-O", "^orphan_file", "-b", "4096",
            "-N", "8192", "-J", "size=4", "-E", "lazy_itable_init=0", pristine])
    base_root = evidence / "root-base.raw"
    VM.run(["cp", "--sparse=always", ROOT / "work/debian-clean-smalljournal.img", base_root])
    VM.run(["tune2fs", "-O", "^orphan_file", base_root], stdout=subprocess.DEVNULL)
    summary = {}
    for stage, name, expected_errno in [(1, "getblk", errno.ENOMEM),
                                         (2, "create_access", errno.EIO),
                                         (3, "dirty_metadata", errno.EIO)]:
        folder = evidence / name
        folder.mkdir()
        summary[name] = {}
        for label, image in [("baseline", before), ("fixed", after)]:
            result = VM.check_vm(folder, image, base_root, pristine, workload, label,
                                 stage=stage, expected_errno=expected_errno)
            runtime = (folder / label / "runtime.txt").read_text()
            assert runtime.count(f"stage={stage} ") == result["injected_failures"]
            serial = (folder / label / "serial.log").read_text(errors="replace")
            assert not any(s in serial for s in ("BUG:", "WARNING: CPU:", "Kernel panic"))
            summary[name][label] = result
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({name: summary[name]}, indent=2), flush=True)
    files = [before, after, evidence / "kernel.config", workload, evidence / patch.name]
    (evidence / "sha256.txt").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files))
    print(f"PASS: {evidence}", flush=True)


if __name__ == "__main__":
    main()
