#!/usr/bin/env python3
"""Save an optional main-project snapshot and verify archived regular files."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile


def digest(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    root = args.root.resolve()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    dest = root / 'experiments/ext4-counter-trace/baselines' / now.strftime('%Y%m%d-%H%M%S%z')
    dest.mkdir(mode=0o700)
    os.chmod(dest, 0o700)
    entries = []
    roots = sorted(p for p in root.iterdir() if p.name != 'experiments')
    # The root-owned staging tree is an external reference, not a full backup.
    def walk(p):
        rel = p.relative_to(root).as_posix()
        if rel == 'work/debian':
            return
        stat = p.lstat()
        e = {'path': rel, 'mode': oct(stat.st_mode & 0o7777),
             'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        if p.is_symlink():
            e.update(type='symlink', target=os.readlink(p))
        elif p.is_dir():
            e['type'] = 'directory'
        elif p.is_file():
            with p.open('rb') as f:
                e.update(type='file', sha256=digest(f))
        else:
            raise RuntimeError('Unsupported baseline entry: ' + rel)
        entries.append(e)
        if e['type'] == 'directory':
            for child in sorted(p.iterdir()):
                walk(child)
    print('Baseline:', dest, flush=True)
    for p in roots:
        print('Hashing:', p.name, flush=True)
        walk(p)
    manifest = dest / 'manifest.jsonl'
    manifest.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in entries))
    paths = dest / 'archive-paths.nul'
    paths.write_bytes(b''.join(os.fsencode(e['path']) + b'\0' for e in entries))
    with (dest / 'files.sha256').open('w') as f:
        for e in entries:
            if e['type'] == 'file':
                f.write(f"{e['sha256']}  {e['path']}\n")
    archive = dest / 'main-project-snapshot.tar.gz'
    print('Creating sparse archive', flush=True)
    subprocess.run(['tar', '--sparse', '-I', 'gzip -1', '-cf', str(archive),
                    '--no-recursion', '--null', '-T', str(paths)], cwd=root, check=True)
    print('Verifying archive contents against manifest', flush=True)
    expected = {e['path']: e for e in entries}
    seen = set()
    with tarfile.open(archive, 'r|gz') as tf:
        for member in tf:
            name = member.name.rstrip('/')
            e = expected[name]
            if e['type'] == 'file':
                assert member.isfile() and member.size == e['size'], name
                assert digest(tf.extractfile(member)) == e['sha256'], name
            elif e['type'] == 'symlink':
                assert member.issym() and member.linkname == e['target'], name
            else:
                assert member.isdir(), name
            seen.add(name)
    assert seen == set(expected), 'Archive membership differs'
    print('Rechecking original files for changes during backup', flush=True)
    for e in entries:
        p = root / e['path']
        if e['type'] == 'file':
            with p.open('rb') as f:
                assert digest(f) == e['sha256'], e['path']
        elif e['type'] == 'symlink':
            assert os.readlink(p) == e['target'], e['path']
    with archive.open('rb') as f:
        archive_hash = digest(f)
    (dest / 'archive.sha256').write_text(f'{archive_hash}  {archive.name}\n')
    report = {'created_at': now.isoformat(), 'source_root': str(root),
              'archive': str(archive), 'archive_sha256': archive_hash,
              'entries': len(entries), 'files': sum(e['type'] == 'file' for e in entries),
              'archive_size_bytes': archive.stat().st_size,
              'verification': 'All archived files match manifest; originals rehashed unchanged',
              'excluded': [{'path': 'work/debian', 'reason': 'Root-owned staging tree contains unreadable directories; original retained',
                            'bootable_rootfs_backup': 'work/debian-clean-smalljournal.img in archive'}]}
    (dest / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (dest / 'README.md').write_text('''# 主线项目快照\n\n归档包含主线项目当时的 kernel/、tests/、tools/、work/（除 work/debian staging tree）、根目录文档及隐藏配置。\n\nwork/debian 是 root 所有的 staging tree，部分目录不可读，因此只作原位置依赖引用；不是已完整备份的目录。可启动 rootfs 镜像、全部现存测试镜像、SSH key 与运行结果已独立归档。备份目录权限为 0700。\n\n校验归档：`sha256sum -c archive.sha256`。manifest.jsonl 记录逐文件内容 hash、路径、权限、时间及 symlink；verification.json 记录完整内容校验结果。\n\n恢复时在独立空目录执行 `tar --sparse -xzf /绝对路径/main-project-snapshot.tar.gz`，再在恢复目录执行 `sha256sum -c /绝对路径/files.sha256`。不要直接覆盖当前项目。staging tree 若需逐字节恢复，仍需有读取权限的单独备份。\n''')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
