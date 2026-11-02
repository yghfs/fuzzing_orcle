#!/usr/bin/env python3
"""Verify the official release, extract without overwriting, and hash the tree."""
import hashlib
import json
import re
import os
from pathlib import Path
import subprocess
import tarfile

base = Path(__file__).resolve().parents[1]
archive = base / 'source/linux-6.12.1.tar.xz'
base.joinpath('source').mkdir(parents=True, exist_ok=True)
base.joinpath('kernel').mkdir(parents=True, exist_ok=True)
if not archive.exists():
    subprocess.run(['python3', str(base/'scripts/download-official.py')], check=True)
expected = next(line.split()[0] for line in (base/'source/sha256sums.asc').read_text().splitlines() if line.endswith('  linux-6.12.1.tar.xz'))
def digest(stream):
    h=hashlib.sha256()
    for block in iter(lambda:stream.read(1024*1024),b''):
        h.update(block)
    return h.hexdigest()
with archive.open('rb') as f:
    assert digest(f)==expected, 'Official tarball SHA256 mismatch'
target = base / 'kernel/linux-6.12.1'
print('Checking official archive paths',flush=True)
with tarfile.open(archive,'r|xz') as tf:
    for m in tf:
        parts=Path(m.name).parts
        assert parts and parts[0]=='linux-6.12.1' and not Path(m.name).is_absolute() and '..' not in parts, m.name
if target.exists():
    print('Existing source tree: verifying without overwriting any files',flush=True)
else:
    subprocess.run(['tar','--same-permissions','-xJf',str(archive),'-C',str(base/'kernel')],check=True)
version={}
for line in (target/'Makefile').read_text().splitlines():
    match=re.fullmatch(r'(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*)',line)
    if match:
        version[match.group(1)]=match.group(2).strip()
assert version=={'VERSION':'6','PATCHLEVEL':'12','SUBLEVEL':'1','EXTRAVERSION':''}, version
print('Comparing extracted files with official archive',flush=True)
entries=[]
with tarfile.open(archive,'r|xz') as tf:
    for m in tf:
        p=base/'kernel'/m.name
        e={'path':m.name,'mode':oct(m.mode)}
        assert p.lstat().st_mode & 0o7777 == m.mode, m.name
        if m.isfile():
            sha=digest(tf.extractfile(m))
            with p.open('rb') as f:
                assert digest(f)==sha,m.name
            assert p.stat().st_size==m.size,m.name
            e.update(type='file',size=m.size,sha256=sha)
        elif m.issym():
            assert p.is_symlink() and os.readlink(p)==m.linkname,m.name
            e.update(type='symlink',target=m.linkname)
        elif m.isdir():
            assert p.is_dir(),m.name
            e['type']='directory'
        else:
            raise RuntimeError('Unexpected release member '+m.name)
        entries.append(e)
actual={p.relative_to(base/'kernel').as_posix() for p in target.rglob('*')}
assert actual=={e['path'].rstrip('/') for e in entries if e['path'].rstrip('/')!='linux-6.12.1'},'Extra or missing files'
(base/'source/pristine-manifest.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in entries))
with (base/'source/pristine-files.sha256').open('w') as out:
    for e in entries:
        if e['type']=='file':
            out.write(f"{e['sha256']}  {e['path']}\n")
report={'version':'6.12.1','archive_sha256':expected,'source_tree':'kernel/linux-6.12.1','entries':len(entries),'regular_files':sum(e['type']=='file' for e in entries),'verification':'All extracted regular files and symlinks match official tarball; no extra files','checker_c_present':(target/'fs/ext4/checker.c').exists(),'config_present':(target/'.config').exists(),'built_image_present':(target/'arch/x86/boot/bzImage').exists()}
(base/'source/verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2),flush=True)
