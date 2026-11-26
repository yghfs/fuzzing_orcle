#!/usr/bin/env python3
"""Verify every EFI source-tree entry against Linux 6.12.1 plus the patch stack."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


def sha_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def official_entries(manifest):
    entries = {}
    for line in manifest.read_text().splitlines():
        entry = json.loads(line)
        if entry['path'] == 'linux-6.12.1':
            continue
        path = entry['path'].removeprefix('linux-6.12.1/')
        if path == entry['path'] or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError(f'invalid official source path: {entry["path"]}')
        if path in entries:
            raise ValueError(f'duplicate official source path: {path}')
        entries[path] = dict(entry, path=path)
    return entries


def apply_layer(entries, files, before_key, after_key):
    """Require every layer to extend the exact previous source version."""
    for file in files:
        path = file['path']
        if Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError(f'invalid instrumentation path: {path}')
        previous = entries.get(path)
        digest = previous.get('sha256') if previous else None
        if (previous and previous['type'] != 'file') or digest != file[before_key]:
            raise ValueError(f'patch source chain mismatch: {path}')
        entries[path] = dict(path=path, type='file', sha256=file[after_key],
                             mode=previous['mode'] if previous else '0o664')


def audit_tree(source, official, expected):
    """Read all files; even hardlinks must match the official/overlay hashes.

    No build artifacts or unlisted local files are accepted in this O= source
    tree. Symlinks are compared without following them. Permission checks cover
    executable bits; umask-dependent read/write bits do not affect code identity.
    """
    errors, actual = [], set()

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            actual.add((Path(directory)/name).relative_to(source).as_posix())
    for path in sorted(actual - expected.keys()):
        errors.append(f'extra:{path}')
    regular_files = symlinks = directories = 0
    for name, entry in sorted(expected.items()):
        path = source/name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            errors.append(f'missing:{name}')
            continue
        kind = entry['type']
        if kind == 'file':
            if not stat.S_ISREG(mode):
                errors.append(f'not_regular_file:{name}')
                continue
            regular_files += 1
            if mode & 0o111 != int(entry['mode'], 8) & 0o111:
                errors.append(f'executable_bits:{name}')
            if sha_file(path) != entry['sha256']:
                errors.append(f'content:{name}')
        elif kind == 'symlink':
            symlinks += 1
            if not stat.S_ISLNK(mode) or os.readlink(path) != entry['target']:
                errors.append(f'symlink:{name}')
        elif kind == 'directory':
            directories += 1
            if not stat.S_ISDIR(mode):
                errors.append(f'not_directory:{name}')
        else:
            raise ValueError(f'unsupported official source entry: {kind}:{name}')
    if errors:
        raise ValueError(f'unreviewed development source ({len(errors)} issues): {errors[:30]}')
    changed = [dict(path=path, upstream=official[path]['sha256'], after=entry['sha256'])
               for path, entry in sorted(expected.items())
               if path in official and entry['type'] == 'file' and
               entry['sha256'] != official[path]['sha256']]
    added = [path for path in sorted(expected) if path not in official]
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True,
                                       separators=(',', ':')).encode()).hexdigest()
    return dict(schema=1, status='pass', kernel_version='6.12.1',
                regular_files=regular_files, symlinks=symlinks, directories=directories,
                upstream_regular_files=sum(e['type'] == 'file' for e in official.values()),
                unchanged_regular_files=regular_files-len(changed)-len(added),
                changed_upstream_files=len(changed), added_files=len(added),
                extra_entries=0, missing_entries=0, mismatched_entries=0,
                expected_tree_sha256=digest, changes=changed, additions=added)


def verify_source(source, project, injection):
    base = project.parent/'ext4-counter-trace'
    manifest = base/'source/pristine-manifest.jsonl'
    official = official_entries(manifest)
    expected = {path: entry.copy() for path, entry in official.items()}
    checker = json.loads((base/'patches/manifest.json').read_text())
    probes = json.loads((project/'sites.json').read_text())
    apply_layer(expected, checker['files'], 'before_sha256', 'after_sha256')
    apply_layer(expected, probes['files'], 'baseline_sha256', 'instrumented_sha256')
    apply_layer(expected, injection['files'], 'before', 'after')
    report = audit_tree(source, official, expected)
    report.update(official_manifest_sha256=sha_file(manifest),
                  patch_stack_sha256=[checker['sha256'], probes['patch_sha256'],
                                      injection['patch_sha256']])
    return report
