#!/usr/bin/env python3
"""Build a shared regular ext4 image and embed it in a syz seed."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile
import uuid
import zlib


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def prepare(seed: int, image: pathlib.Path, template: pathlib.Path,
            program: pathlib.Path, metadata: pathlib.Path) -> dict:
    if image.exists() or program.exists() or metadata.exists():
        raise FileExistsError('comparison seed outputs already exist')
    image.parent.mkdir(parents=True, exist_ok=True)
    program.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)

    image_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                f'ext4-counter-campaign:{seed}:regular'))
    with tempfile.TemporaryDirectory(prefix='.comparison-ext4-', dir=image.parent) as staging:
        staged_image = pathlib.Path(staging) / 'regular.raw'
        run('qemu-img', 'create', '-q', '-f', 'raw', str(staged_image), '132M')
        run('mke2fs', '-q', '-F', '-t', 'ext4', '-O', 'orphan_file', '-b', '4096',
            '-N', '4096', '-E', f'lazy_itable_init=0,hash_seed={image_uuid}',
            '-U', image_uuid, str(staged_image))

        content_root = pathlib.Path(staging) / 'content'
        content_root.mkdir()
        commands = ['mkdir /syz-work']
        for n in range(4):
            commands.append(f'mkdir /syz-work/file{n}')
        commands += ['mkdir /syz-work/dir0', 'mkdir /syz-work/empty0',
                     'mkdir /syz-work/srcdir', 'mkdir /syz-work/dstdir']
        for name in ('syz-work/file0/file0', 'syz-work/file1/data',
                     'syz-work/dir0/file0', 'syz-work/source',
                     'syz-work/replacement', 'syz-work/exchange0',
                     'syz-work/exchange1'):
            source = content_root / name.replace('/', '_')
            source.write_bytes(b'counter-trace-seed\n')
            commands.append(f'write {source} /{name}')
        command_file = pathlib.Path(staging) / 'debugfs.commands'
        command_file.write_text('\n'.join(commands) + '\n', encoding='utf-8')
        run('debugfs', '-w', '-f', str(command_file), str(staged_image))
        staged_image.replace(image)

    original = template.read_text(encoding='utf-8')
    # The mount-image chdir flag must stay clear so r0 remains the valid
    # directory fd returned by syz_mount_image. The paths below target the
    # exact regular image that is also passed to the custom campaign.
    mount_pattern = r'(syz_mount_image\$ext4\([^\n]*?,\s*0x0,\s*&AUTO,\s*)0x1(,\s*AUTO,\s*&AUTO=")\$e[^\"]+("\))'
    updated, count = re.subn(mount_pattern, r'\g<1>0x0\g<2>$eIMAGE\3', original, count=1)
    if count != 1:
        image_pattern = r'&AUTO="\$e[^\"]+"'
        updated, count = re.subn(image_pattern, '&AUTO="$eIMAGE"', original, count=1)
        if count != 1:
            raise ValueError('could not find compressed syz_mount_image payload')
        updated = re.sub(r'(syz_mount_image\$ext4\([^\n]*?,\s*0x0,\s*&AUTO,\s*)0x1',
                         r'\g<1>0x0', updated, count=1)
    updated, count = re.subn(
        r'(syz_mount_image\$ext4\([^\n]*?,\s*)0x0(,\s*&AUTO,\s*0x0,)',
        r'\g<1>0x400\g<2>', updated, count=1)
    if count != 1:
        raise ValueError('could not align syz_mount_image flags with the custom mount')
    encoded = base64.b64encode(zlib.compress(image.read_bytes(), level=9)).decode('ascii')
    if updated.count('$eIMAGE') != 1:
        raise ValueError('seed image placeholder is not unique')
    updated = updated.replace('$eIMAGE', '$' + encoded, 1)
    program.write_text(updated, encoding='utf-8')

    payload = {
        'schema': 1,
        'seed': seed,
        'filesystem': 'ext4',
        'kind': 'regular',
        'image': str(image.resolve()),
        'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
        'image_bytes': image.stat().st_size,
        'seed_program': str(program.resolve()),
        'seed_program_sha256': hashlib.sha256(program.read_bytes()).hexdigest(),
        'mount_chdir': False,
        'initial_tree': 'syz-work/file0/file0 plus the standard campaign namespace tree',
    }
    metadata.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--image', type=pathlib.Path, required=True)
    parser.add_argument('--template', type=pathlib.Path, required=True)
    parser.add_argument('--program', type=pathlib.Path, required=True)
    parser.add_argument('--metadata', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seed <= 2**31 - 1:
        parser.error('--seed must be in [1, 2147483647]')
    print(json.dumps(prepare(args.seed, args.image, args.template, args.program,
                             args.metadata), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
