#!/usr/bin/env python3
"""Download and verify the official Linux 6.12.1 source archive."""
import datetime
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

base = Path(__file__).resolve().parents[1] / 'source'
url = 'https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.1.tar.xz'
size = 147912528
existing = base / 'linux-6.12.1.tar.xz'
expected_existing = next(line.split()[0] for line in (base/'sha256sums.asc').read_text().splitlines() if line.endswith('  linux-6.12.1.tar.xz'))
if existing.exists():
    h_existing = hashlib.sha256()
    with existing.open('rb') as f_existing:
        for block_existing in iter(lambda: f_existing.read(1024 * 1024), b''):
            h_existing.update(block_existing)
    if h_existing.hexdigest() == expected_existing:
        print('Existing official tarball checksum verified; no download needed.')
        sys.exit(0)
started = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
target = base / 'linux-6.12.1.tar.xz'
partial = target.with_suffix('.tar.xz.part')
digest = hashlib.sha256()
with urllib.request.urlopen(url, timeout=60) as response, partial.open('wb') as out:
    total = 0
    while block := response.read(1024 * 1024):
        total += len(block)
        digest.update(block)
        out.write(block)
        if total % (16 * 1024 * 1024) < 1024 * 1024:
            print(f'Official download: {total}/{size} bytes', flush=True)
expected = next(line.split()[0] for line in (base/'sha256sums.asc').read_text().splitlines() if line.endswith('  linux-6.12.1.tar.xz'))
assert total == size, f'unexpected download length: {total} (expected {size})'
assert digest.hexdigest() == expected, (digest.hexdigest(), expected)
partial.replace(target)
(base/'linux-6.12.1.tar.xz.sha256').write_text(f'{expected}  linux-6.12.1.tar.xz\n')
(base/'provenance.json').write_text(json.dumps({'url':url,'checksum_url':'https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc','download_started_at':started,'download_completed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),'size_bytes':size,'sha256':expected,'official_checksum_match':True,'pgp_signature_verified':False,'method':'HTTPS download verified against the official SHA256 list'},indent=2)+'\n')
print('Official SHA256 matches:',expected,flush=True)
