#!/usr/bin/env python3
"""Write a manager configuration with paths for this checkout."""

import argparse
import json
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
if "vm" in config:
	config["kernel_obj"] = str(root / "experiments/ext4-invariant-checker/build/linux-invariant-checker")
	config["vm"]["kernel"] = str(
		root / "experiments/ext4-invariant-checker/build/linux-invariant-checker/arch/x86/boot/bzImage"
	)
	config["image"] = str(root / "work/debian.img")
	config["sshkey"] = str(root / "work/debian.id_rsa")
	config["syzkaller"] = str(root / "tools/syzkaller")
	config["workdir"] = str(root / "work/syzkaller" / source.stem)
	if "http" in config:
		config["http"] = "127.0.0.1:56742"
if "workdir" in config:
	Path(config["workdir"]).mkdir(parents=True, exist_ok=True)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(config, indent=2) + "\n")
print(target)
