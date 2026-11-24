#!/usr/bin/env python3
"""Reviewed source inventory and fail-closed build binding for ext4 probes.

Lexical candidate discovery is a drift guard, not a C proof. All production
ext4 source/header hashes are pinned so syntax outside these searches cannot
silently enter an approved long-run kernel. Refresh requires source review.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import re
import sys
import tempfile

PROJECT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OBJ = pathlib.Path('build/linux-counter-trace-normal')
PATTERNS = {
    'itable_highwater': r'\b(?:ext4_ct_)?(?:ext4_)?itable_unused_set\s*\(',
    'xattr_reference': r'\bi_file_acl\s*=(?!=)|\bh_refcount\s*=(?!=)',
    'orphan_slot': r'\bbdata\[[^]]+\]\s*=(?!=)|\bcmpxchg\(&bdata\[',
    'orphan_free': r'\batomic_(?:inc|dec_if_positive|set)\([^;]*?ob_free_entries[^;]*;',
    'descriptor': r'\b(?:ext4_ct_)?(?:ext4_)?(?:free_group_clusters|free_inodes|used_dirs)_set\s*\(',
    'global': r'\b(?:percpu_counter_(?:add|sub|inc|dec|set|init)|ext4_ct_global_add)\s*\([^;]*?s_(?:freeclusters|freeinodes|dirs)_counter[^;]*;',
    'nlink': r'\b(?:set_nlink|clear_nlink|inc_nlink|drop_nlink|ext4_ct_nlink)\s*\(',
    'bitmap': r'\b(?:ext4_(?:test_and_)?(?:set|clear)_bit|mb_(?:set|clear)_bits|ext4_mark_bitmap_end)\s*\(',
    'flags': r'\bext4_ct_group_flags_set\s*\(|\b\w+->bg_flags\s*(?:=(?!=)|[|&]=)',
    'dirent': r'\b(?:de\w*|\w+\.de|\w+->de|\w+->parent_de)->inode\s*=(?!=)',
    'descriptor_fields': r'\bbg->bg_(?:free_blocks_count|free_inodes_count|used_dirs_count)_(?:lo|hi)\s*=',
    'super_summary': r'\bext4_free_blocks_count_set\s*\(|\b(?:sbi->s_es|es)->s_free_inodes_count\s*=(?!=)|\ble32_add_cpu\(&es->s_free_inodes_count',
}


def digest(path):
    sha256 = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha256.update(block)
    return sha256.hexdigest()


def clean(text):
    # Keep line coordinates; do not mistake comments/strings for write sites.
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), text, flags=re.S)


def discover(source):
    source = pathlib.Path(source)
    files = sorted((source/'fs/ext4').glob('*.c')) + sorted((source/'fs/ext4').glob('*.h'))
    files += sorted((source/'include/trace/events').glob('ext4*.h'))
    files += [source/path for path in ('fs/ext4/Kconfig', 'fs/ext4/Makefile',
                                       'fs/jbd2/journal.c', 'include/linux/jbd2.h')]
    hashes, sites = {}, []
    for path in files:
        rel = path.relative_to(source).as_posix()
        hashes[rel] = digest(path)
        if path.suffix != '.c':
            continue
        text = path.read_text()
        code = clean(text)
        for kind, pattern in PATTERNS.items():
            for m in re.finditer(pattern, code):
                line = code.count('\n', 0, m.start()) + 1
                excerpt = ' '.join(text[m.start():text.find('\n', m.start())].strip().split())
                sites.append({'file': rel, 'line': line, 'kind': kind, 'source': excerpt})
    return {'schema': 1, 'scope': 'Linux 6.12.1 ext4 production authority paths; lexical drift guard',
            'source_sha256': hashes, 'sites': sorted(sites, key=lambda s:(s['file'],s['line'],s['kind']))}


def build_artifacts(project, obj):
    return {'inventory': project/'audit/source-inventory.json',
            'kernel': obj/'arch/x86/boot/bzImage',
            'config': obj/'.config',
            'patch': project/'patches/0001-counter-trace-core-and-probes.patch'}


def verify(project=PROJECT, require_build=False, kernel_obj=None):
    project = pathlib.Path(project)
    source = project/'dev/linux-6.12.1'
    path = project/'audit/source-inventory.json'
    if not path.exists():
        return ['missing_reviewed_source_inventory']
    expected = json.loads(path.read_text())
    current = discover(source)
    issues = []
    if current != expected:
        for file in sorted(set(current['source_sha256']) | set(expected['source_sha256'])):
            if current['source_sha256'].get(file) != expected['source_sha256'].get(file):
                issues.append('unreviewed_source_change:'+file)
        if current['sites'] != expected['sites']:
            issues.append('write_site_inventory_changed')
    if require_build:
        obj = pathlib.Path(kernel_obj) if kernel_obj is not None else project/DEFAULT_OBJ
        binding = obj/'build-binding.json'
        if not binding.exists():
            return issues + ['missing_audited_build_binding']
        data = json.loads(binding.read_text())
        for key, artifact in build_artifacts(project, obj).items():
            if not artifact.exists() or data.get(key) != digest(artifact):
                issues.append('audited_build_binding_changed:'+key)
    return issues


def bind_build(obj):
    artifacts = build_artifacts(PROJECT, obj)
    missing = ['missing_build_artifact:'+key for key, path in artifacts.items()
               if not path.is_file()]
    if missing:
        return missing
    data = {key: digest(path) for key, path in artifacts.items()}
    manifest = json.loads((PROJECT/'patches/manifest.json').read_text())
    if manifest.get('sha256') != data['patch'] or not manifest.get('replay_verified'):
        return ['exported_patch_manifest_mismatch']
    with tempfile.NamedTemporaryFile(mode='w', dir=obj, delete=False) as stream:
        stream.write(json.dumps(data, indent=2)+'\n')
    pathlib.Path(stream.name).replace(obj/'build-binding.json')
    return []


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--write-inventory', action='store_true', help='maintainer-only: pin reviewed source after source audit')
    p.add_argument('--bind-build', action='store_true', help='bind successfully built kernel/config/exported patch to reviewed source')
    p.add_argument('--require-build', action='store_true')
    p.add_argument('--kernel-obj', type=pathlib.Path, default=PROJECT/DEFAULT_OBJ,
                   help='kernel output directory containing bzImage, config and local binding')
    a = p.parse_args()
    if a.write_inventory:
        data = discover(PROJECT/'dev/linux-6.12.1')
        (PROJECT/'audit/source-inventory.json').write_text(json.dumps(data, indent=2)+'\n')
    issues = verify(require_build=a.require_build and not a.bind_build, kernel_obj=a.kernel_obj)
    if a.bind_build and not issues:
        issues = bind_build(a.kernel_obj)
    print(json.dumps({'passed': not issues, 'issues': issues}, indent=2))
    return bool(issues)

if __name__ == '__main__':
    sys.exit(main())
