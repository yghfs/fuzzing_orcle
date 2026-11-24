#!/usr/bin/env python3
"""Write a manager configuration with paths for this checkout."""

import argparse
import json
import os
from pathlib import Path


root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("config", type=Path, help="tracked syzkaller manager JSON")
parser.add_argument("--output", type=Path, help="local output path (defaults to CONFIG.local.cfg)")
args = parser.parse_args()
source = args.config if args.config.is_absolute() else root / args.config
target = args.output or source.with_name(source.stem + ".local" + source.suffix)
if not target.is_absolute():
    target = root / target
config = json.loads(source.read_text())
old_root = "/home/ikun/fuzzing-orcle/"


def relocate(value):
    if isinstance(value, dict):
        return {key: relocate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item) for item in value]
    if isinstance(value, str) and value.startswith(old_root):
        return str(root / value.removeprefix(old_root))
    return value


config = relocate(config)


def local_path(variable, configured, default):
    return str(Path(os.environ.get(variable) or configured or default).resolve())


if config.get("type") == "qemu":
    counter = root / "experiments/ext4-counter-trace"
    vm = config.setdefault("vm", {})
    build = local_path("FUZZING_ORCLE_KERNEL_OBJ", config.get("kernel_obj"),
                       counter / "build/linux-counter-trace-normal")
    config["kernel_obj"] = build
    if "kernel_src" not in config and "kernel" in vm:
        config["kernel_src"] = build
    config["kernel_src"] = local_path("FUZZING_ORCLE_KERNEL", config.get("kernel_src"),
                                      counter / "dev/linux-6.12.1")
    configured_kernel = None if os.environ.get("FUZZING_ORCLE_KERNEL_OBJ") else vm.get("kernel")
    vm["kernel"] = local_path("FUZZING_ORCLE_BZIMAGE", configured_kernel,
                              Path(build) / "arch/x86/boot/bzImage")
    config["image"] = local_path("FUZZING_ORCLE_ROOTFS", config.get("image"),
                                 root / "work/debian-normal.img")
    config.setdefault("sshkey", str(root / "work/debian.id_rsa"))
    config.setdefault("syzkaller", str(root / "tools/syzkaller"))
    config.setdefault("workdir", str(root / "work/syzkaller" / source.stem))
if "workdir" in config:
    Path(config["workdir"]).mkdir(parents=True, exist_ok=True)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(config, indent=2) + "\n")
print(target)
