import importlib.util
import base64
import pathlib
import tempfile
import unittest
import zlib


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'validate-upstream-seed-execution.py'
SPEC = importlib.util.spec_from_file_location('validate_upstream_seed_execution', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidateUpstreamSeedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.seed = self.root / 'seed.syz'
        self.seed.write_text('''r0 = syz_mount_image$ext4(&AUTO, &AUTO, 0x0, &AUTO, 0x0, AUTO, &AUTO="payload")
r1 = openat(r0, &AUTO, 0x0, 0x0)
write(r1, &AUTO, 0x4)
close(r1)
close(r0)
''')
        self.log = self.root / 'manager.log'

    def tearDown(self):
        self.temp.cleanup()

    def test_requires_successful_mount_and_all_seed_calls_in_order(self):
        self.log.write_text('''#0 -> syz_mount_image$ext4(0x1)
syz_mount_image: loop='/dev/loop1' fs='ext4'
EXT4-fs (loop1): mounted filesystem 123 r/w
#0 <- syz_mount_image$ext4=0x3 cover=10
#1 -> openat(0x3)
#1 <- openat=0x4 cover=10
#2 -> write(0x4)
#2 <- write=0x4 cover=10
#3 -> close(0x4)
#3 <- close=0x0 cover=10
#4 -> close(0x3)
#4 <- close=0x0 cover=10
EXT4-fs (loop1): unmounting filesystem 123.
''')
        report = MODULE.validate(self.log, self.seed)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['target_filesystem_execution_verified'])
        self.assertEqual(report['selected_mount']['matched_calls'], 5)

    def test_mount_or_operation_failure_fails_execution_gate(self):
        self.log.write_text('''#0 -> syz_mount_image$ext4(0x1)
syz_mount_image: loop='/dev/loop1' fs='ext4'
EXT4-fs (loop1): mounted filesystem 123 r/w
#0 <- syz_mount_image$ext4=0x3
#1 -> openat(0x3)
#1 <- openat=0x4
#2 -> write(0x4)
#2 <- write=0xffffffffffffffff errno=5
#3 -> close(0x4)
#3 <- close=0x0
#4 -> close(0x3)
#4 <- close=0x0
EXT4-fs (loop1): unmounting filesystem 123.
''')
        report = MODULE.validate(self.log, self.seed)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['target_filesystem_execution_verified'])

    def test_non_ext4_mount_is_not_accepted(self):
        self.log.write_text('''#0 -> syz_mount_image$ext4(0x1)
#0 <- syz_mount_image$ext4=0x3
#1 -> openat(0x3)
#1 <- openat=0x4
#2 -> write(0x4)
#2 <- write=0x4
#3 -> close(0x4)
#3 <- close=0x0
#4 -> close(0x3)
#4 <- close=0x0
''')
        report = MODULE.validate(self.log, self.seed)
        self.assertEqual(report['successful_mounts'], 0)
        self.assertFalse(report['target_filesystem_execution_verified'])

    def test_embedded_image_must_match_the_requested_target_image(self):
        image_bytes = b'ext4 target image bytes'
        image = self.root / 'target.raw'
        image.write_bytes(image_bytes)
        payload = base64.b64encode(zlib.compress(image_bytes)).decode('ascii')
        self.seed.write_text(f'''r0 = syz_mount_image$ext4(&AUTO, &AUTO, 0x0, &AUTO, 0x0, AUTO, &AUTO="${payload}")
r1 = openat(r0, &AUTO, 0x0, 0x0)
write(r1, &AUTO, 0x4)
close(r1)
close(r0)
''')
        self.log.write_text('''#0 -> syz_mount_image$ext4(0x1)
syz_mount_image: loop='/dev/loop1' fs='ext4'
EXT4-fs (loop1): mounted filesystem 123 r/w
#0 <- syz_mount_image$ext4=0x3
#1 -> openat(0x3)
#1 <- openat=0x4
#2 -> write(0x4)
#2 <- write=0x4
#3 -> close(0x4)
#3 <- close=0x0
#4 -> close(0x3)
#4 <- close=0x0
EXT4-fs (loop1): unmounting filesystem 123.
''')
        matching = MODULE.validate(self.log, self.seed, image)
        self.assertEqual(matching['status'], 'passed')
        self.assertTrue(matching['seed_embedded_image_matches_target'])
        image.write_bytes(b'different target image')
        mismatching = MODULE.validate(self.log, self.seed, image)
        self.assertEqual(mismatching['status'], 'failed')
        self.assertFalse(mismatching['target_filesystem_execution_verified'])


if __name__ == '__main__':
    unittest.main()
