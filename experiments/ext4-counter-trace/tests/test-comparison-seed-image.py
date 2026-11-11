#!/usr/bin/env python3
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile
import unittest
import base64
import zlib


PROJECT = pathlib.Path(__file__).resolve().parents[3]
EXPERIMENT = pathlib.Path(__file__).resolve().parents[1]
PREPARE = EXPERIMENT / 'scripts' / 'prepare-comparison-ext4-seed.py'
TEMPLATE = EXPERIMENT / 'syzgen' / 'seeds' / 'upstream-ext4.syz'
PROG2C = PROJECT / 'tools/src/syzkaller-master/bin/syz-prog2c'


class ComparisonSeedImageTests(unittest.TestCase):
    def test_image_and_native_seed_are_consistent_and_parseable(self):
        with tempfile.TemporaryDirectory(prefix='comparison-seed-test-') as tmp:
            root = pathlib.Path(tmp)
            image = root / 'regular.raw'
            program = root / 'upstream-ext4.syz'
            metadata = root / 'metadata.json'
            subprocess.run([
                'python3', str(PREPARE), '--seed', '20261104', '--image', str(image),
                '--template', str(TEMPLATE), '--program', str(program), '--metadata', str(metadata),
            ], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['e2fsck', '-fn', str(image)], check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run([str(PROG2C), '-os', 'linux', '-arch', 'amd64',
                            '-prog', str(program), '-strict'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            evidence = json.loads(metadata.read_text())
            seed_text = program.read_text()
            mount_line = next(line for line in seed_text.splitlines()
                              if 'syz_mount_image$ext4' in line)
            self.assertIn(', 0x400, &AUTO, 0x0, AUTO, &AUTO=', mount_line)
            self.assertEqual(evidence['image_sha256'],
                             hashlib.sha256(image.read_bytes()).hexdigest())
            payload = re.search(r'&AUTO="\$([A-Za-z0-9+/=]+)"', seed_text)
            self.assertIsNotNone(payload)
            encoded = payload.group(1)
            encoded += '=' * (-len(encoded) % 4)
            self.assertEqual(zlib.decompress(base64.b64decode(encoded)),
                             image.read_bytes())
            self.assertEqual(evidence['mount_chdir'], False)
            self.assertIn("'syz-work/file0/file0\\x00'", seed_text)
            self.assertIn("'syz-work/file0/renamed\\x00'", seed_text)


if __name__ == '__main__':
    unittest.main()
