#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
import sys
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / 'build/ct-syzgen'
sys.path.insert(0, str(ROOT / 'scripts'))
from syz_workload import instrument, execution_issues


class LauncherTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, 'cleanup failure requires an unprivileged user')
    def test_failed_cleanup_closes_phase(self):
        with tempfile.TemporaryDirectory(prefix='ct-launcher-cleanup-') as temp:
            temp = pathlib.Path(temp)
            trace = temp / 'tracing'
            trace.mkdir()
            (trace / 'trace_marker').touch()
            for event in ('sys_enter', 'sys_exit'):
                folder = trace / 'events/raw_syscalls' / event
                folder.mkdir(parents=True)
                (folder / 'filter').touch()
            source = (ROOT / 'tests/syz-launcher.c').read_text()
            source = source.replace('/sys/kernel/tracing', str(trace))
            # A regular file needs append semantics to model trace_marker's
            # event stream; the real tracing endpoint appends each write.
            source = source.replace('trace_marker", O_WRONLY | O_CLOEXEC',
                                    'trace_marker", O_WRONLY | O_APPEND | O_CLOEXEC')
            (temp / 'launcher.c').write_text(source)
            launcher = temp / 'launcher'
            subprocess.run(['gcc', '-O2', '-o', str(launcher), str(temp / 'launcher.c')],
                           check=True)
            root = temp / 'root'
            shared_root = root / 'syz-work' / 'shared'
            shared_root.mkdir(parents=True)
            (shared_root / 'locked-entry').write_text('keep')
            shared_root.chmod(0o500)
            try:
                cleanup = subprocess.run(
                    [str(launcher), '--cleanup', str(root)],
                    cwd=temp, capture_output=True, text=True, timeout=15)
                self.assertEqual(cleanup.returncode, 90, cleanup.stderr)
                markers = (trace / 'trace_marker').read_text()
                self.assertEqual(markers.count('CT_PHASE phase=BEGIN source=cleanup'), 1)
                self.assertEqual(markers.count('CT_PHASE phase=END source=cleanup'), 1)
            finally:
                shared_root.chmod(0o700)

    def test_namespace_call_identity_uses_relative_parent_directory_device(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory(prefix='ct-namespace-identity-') as temp:
            source = pathlib.Path(temp) / 'namespace-identity.c'
            binary = pathlib.Path(temp) / 'namespace-identity'
            source.write_text(runtime + r'''
#include <assert.h>
int ioctl(int fd, unsigned long request, ...) { (void)fd; (void)request; return 0; }
int main(void)
{
  char root[] = "/tmp/ct-ns-identity-XXXXXX";
  assert(mkdtemp(root));
  assert(chdir(root) == 0);
  int rootfd = open(root, O_RDONLY | O_DIRECTORY);
  assert(rootfd >= 0);
  struct stat cwd;
  assert(fstat(AT_FDCWD, &cwd) < 0); /* AT_FDCWD is a sentinel, not an fd. */
  unsigned long long ino, dev;
  int is_dir;
  long mkdir_args[6] = {AT_FDCWD, (long)"child", 0755, 0, 0, 0};
  ct_inode_identity("mkdirat", mkdir_args, &ino, &dev, &is_dir);
  assert(ino != 0 && dev != 0 && is_dir == 1);
  assert(mkdir("child", 0755) == 0);
  long dir_open_args[6] = {rootfd, (long)"child", O_RDONLY | O_DIRECTORY, 0, 0, 0};
  ct_inode_identity("openat$dir", dir_open_args, &ino, &dev, &is_dir);
  assert(ino != 0 && is_dir == 1);
  struct stat root_stat;
  assert(fstat(rootfd, &root_stat) == 0 && (dev_t)dev == root_stat.st_dev);
  struct stat child_stat;
  assert(fstatat(rootfd, "child", &child_stat, 0) == 0);
  assert(ino == child_stat.st_ino && (dev_t)dev == child_stat.st_dev);
  long regular_open_args[6] = {rootfd, (long)"child/file", O_CREAT | O_RDWR, 0600, 0, 0};
  ct_inode_identity("openat", regular_open_args, &ino, &dev, &is_dir);
  assert(ino != 0 && is_dir == 1 && (dev_t)dev == root_stat.st_dev);
  int filefd = openat(rootfd, "child/file", O_CREAT | O_RDWR, 0600);
  assert(filefd >= 0);
  struct stat file_stat;
  assert(fstat(filefd, &file_stat) == 0);
  long existing_open_args[6] = {rootfd, (long)"child/file", O_RDWR, 0, 0, 0};
  ct_inode_identity("openat", existing_open_args, &ino, &dev, &is_dir);
  assert(ino == file_stat.st_ino && (dev_t)dev == file_stat.st_dev && is_dir == 0);
  long read_args[6] = {filefd, 0, 0, 0, 0, 0};
  ct_inode_identity("read", read_args, &ino, &dev, &is_dir);
  assert(fstat(filefd, &file_stat) == 0 && ino == file_stat.st_ino && (dev_t)dev == file_stat.st_dev);
  ct_inode_identity("close", read_args, &ino, &dev, &is_dir);
  assert(ino == file_stat.st_ino && (dev_t)dev == file_stat.st_dev);
  long rename_args[6] = {rootfd, (long)"child/file", rootfd,
                         (long)"child/renamed", 0, 0};
  ct_inode_identity("renameat2", rename_args, &ino, &dev, &is_dir);
  assert(ino != 0 && dev != 0 && is_dir == 1 && (dev_t)dev == root_stat.st_dev);
  long link_args[6] = {rootfd, (long)"child/renamed", rootfd,
                       (long)"child/linked", 0, 0};
  ct_inode_identity("linkat", link_args, &ino, &dev, &is_dir);
  assert(ino != 0 && dev != 0 && is_dir == 1 && (dev_t)dev == root_stat.st_dev);
  long symlink_args[6] = {(long)"renamed", rootfd, (long)"child/sym", 0, 0, 0};
  ct_inode_identity("symlinkat", symlink_args, &ino, &dev, &is_dir);
  assert(ino != 0 && dev != 0 && is_dir == 1 && (dev_t)dev == root_stat.st_dev);
  assert(symlinkat("file", rootfd, "child/sym") == 0);
  long nofollow_args[6] = {rootfd, (long)"child/sym", O_RDONLY | O_NOFOLLOW, 0, 0, 0};
  ct_inode_identity("openat", nofollow_args, &ino, &dev, &is_dir);
  struct stat symlink_stat;
  assert(lstat("child/sym", &symlink_stat) == 0);
  assert(ino == symlink_stat.st_ino && (dev_t)dev == symlink_stat.st_dev && is_dir == 0);
  long follow_args[6] = {rootfd, (long)"child/sym", O_RDONLY, 0, 0, 0};
  ct_inode_identity("openat", follow_args, &ino, &dev, &is_dir);
  assert(ino == file_stat.st_ino && (dev_t)dev == file_stat.st_dev && is_dir == 0);
  long unlink_args[6] = {rootfd, (long)"child/linked", 0, 0, 0, 0};
  ct_inode_identity("unlinkat", unlink_args, &ino, &dev, &is_dir);
  assert(ino != 0 && is_dir == 1 && (dev_t)dev == root_stat.st_dev);
  long rmdir_args[6] = {(long)"child", 0, 0, 0, 0, 0};
  ct_inode_identity("rmdir", rmdir_args, &ino, &dev, &is_dir);
  assert(ino != 0 && is_dir == 1 && (dev_t)dev == root_stat.st_dev);
  struct stat dir;
  assert(stat(".", &dir) == 0);
  assert(root_stat.st_ino == dir.st_ino && root_stat.st_dev == dir.st_dev);
  long absolute_args[6] = {AT_FDCWD, (long)"/tmp/other", 0, 0, 0, 0};
  ct_inode_identity("mkdirat", absolute_args, &ino, &dev, &is_dir);
  struct stat tmpdir;
  assert(stat("/tmp", &tmpdir) == 0);
  assert(ino != 0 && (dev_t)dev == tmpdir.st_dev && is_dir == 1);
  long invalid_unlink_args[6] = {rootfd, -1, 0, 0, 0, 0};
  ct_inode_identity("unlinkat", invalid_unlink_args, &ino, &dev, &is_dir);
  assert(ino == 0 && dev == 0 && is_dir == 0);
  long invalid_open_args[6] = {rootfd, 0x7fffdead0000L, O_RDONLY, 0, 0, 0};
  ct_inode_identity("openat", invalid_open_args, &ino, &dev, &is_dir);
  assert(ino == 0 && dev == 0 && is_dir == 0);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


class SyzkallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global GEN
        temp = tempfile.TemporaryDirectory(prefix='ct-syzgen-test-')
        cls.addClassCleanup(temp.cleanup)
        GEN = pathlib.Path(temp.name) / 'ct-syzgen'
        go = (__import__('os').environ.get('FUZZING_ORCLE_GO') or
              __import__('shutil').which('go') or
              str(ROOT.parents[1] / 'tools/go126/bin/go1.26.0'))
        env = dict(__import__('os').environ, GOFLAGS='-buildvcs=false')
        subprocess.run([str(go), '-C', str(ROOT / 'syzgen'), 'build', '-mod=readonly', '-o', str(GEN), '.'],
                       env=env,
                       check=True, timeout=120)

    def test_seed_is_reproducible_and_allowlist_is_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = pathlib.Path(temp) / 'a', pathlib.Path(temp) / 'b'
            for out in (a, b):
                subprocess.run([str(GEN), '-out', str(out), '-seed', '314159', '-calls', '12'], check=True)
            self.assertEqual((a / 'program.syz').read_bytes(), (b / 'program.syz').read_bytes())
            data = json.loads((a / 'program.json').read_text())
            allowed = set(data['allowlist'])
            self.assertTrue(data['calls'])
            self.assertEqual(len(data['calls']), 12)
            self.assertTrue(all(call['name'] in allowed for call in data['calls']))
            self.assertNotIn('mknod', allowed)

    def test_generated_program_requires_data_modification_call(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp) / 'program'
            subprocess.run([str(GEN), '-out', str(out), '-seed', '314159', '-calls', '12'], check=True)
            data = json.loads((out / 'program.json').read_text())
            self.assertEqual(len(data['calls']), 12)
            data_calls = {'write', 'pwrite64', 'fallocate', 'ftruncate', 'fsync', 'fdatasync'}
            self.assertTrue(data['required_data_call'])
            self.assertTrue(data['has_data_call'])
            self.assertTrue(any(call['name'] in data_calls for call in data['calls']))
            self.assertGreaterEqual(data['accepted_attempt'], 1)

    def test_export_is_instrumentable_one_marker_per_generated_call(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp) / 'program'
            subprocess.run([str(GEN), '-out', str(out), '-seed', '2718', '-calls', '8'], check=True)
            meta = json.loads((out / 'program.json').read_text())
            runtime = (ROOT / 'tests/syz-runtime.h').read_text()
            source = instrument((out / 'original.c').read_text(), meta, runtime)
            self.assertEqual(source.count('ct_call('), len(meta['calls']) + 1)
            (out / 'workload.c').write_text(source)
            subprocess.run(['gcc', '-O2', '-static', '-pthread', '-o', str(out / 'workload'), str(out / 'workload.c')], check=True)

    def test_runtime_sets_distinct_saturation_cause_flags(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'kcov-causes.c'
            binary = pathlib.Path(temp) / 'kcov-causes'
            source.write_text(runtime + r'''
#include <assert.h>
int ioctl(int fd, unsigned long request, ...) { (void)fd; (void)request; return 0; }
int main(void)
{
  ct_kcov_fd = 1;
  ct_kcov_spool = tmpfile();
  assert(ct_kcov_spool);
  ct_kcov_entries = 3;
  ct_kcov_area = calloc(ct_kcov_entries + 1, sizeof(uint64_t));
  assert(ct_kcov_area);
  ct_kcov_area[0] = 3;
  ct_kcov_area[1] = 11; ct_kcov_area[2] = 12; ct_kcov_area[3] = 13;
  ct_kcov_active = 1;
  ct_kcov_current_call = 0;
  ct_kcov_call_count = 1;
  ct_kcov_calls[0].start = 0;
  ct_kcov_stop();
  assert(ct_kcov_saturated);
  assert(ct_kcov_saturation_reasons == CT_KCOV_FLAG_PER_CALL_FULL);
  assert(ct_kcov_count == 3);
  fclose(ct_kcov_spool);

  ct_kcov_spool = tmpfile();
  assert(ct_kcov_spool);
  ct_kcov_saturated = 0;
  ct_kcov_saturation_reasons = 0;
  ct_kcov_count = CT_KCOV_MAX_TOTAL_PCS - 2;
  assert(ftruncate(fileno(ct_kcov_spool), ct_kcov_count * 2 * sizeof(uint64_t)) == 0);
  assert(lseek(fileno(ct_kcov_spool), ct_kcov_count * 2 * sizeof(uint64_t), SEEK_SET) >= 0);
  ct_kcov_entries = 16;
  ct_kcov_area = calloc(ct_kcov_entries + 1, sizeof(uint64_t));
  assert(ct_kcov_area);
  ct_kcov_area[0] = 5;
  for (unsigned i = 1; i <= 5; i++) ct_kcov_area[i] = 100 + i;
  ct_kcov_active = 1;
  ct_kcov_current_call = 1;
  ct_kcov_call_count = 2;
  ct_kcov_calls[1].start = ct_kcov_count;
  ct_kcov_stop();
  assert(ct_kcov_saturated);
  assert(ct_kcov_saturation_reasons == CT_KCOV_FLAG_TOTAL_LIMIT);
  assert(ct_kcov_count == CT_KCOV_MAX_TOTAL_PCS);
  fclose(ct_kcov_spool);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-pthread', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_runtime_compacts_repeated_pcs_without_losing_self_edge(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'kcov-compact.c'
            binary = pathlib.Path(temp) / 'kcov-compact'
            source.write_text(runtime + r'''
#include <assert.h>
int ioctl(int fd, unsigned long request, ...) { (void)fd; (void)request; return 0; }
int main(void)
{
  uint64_t token[2];
  ct_kcov_fd = 1;
  ct_kcov_spool = tmpfile();
  assert(ct_kcov_spool);
  ct_kcov_entries = 16;
  ct_kcov_area = calloc(ct_kcov_entries + 1, sizeof(uint64_t));
  assert(ct_kcov_area);
  ct_kcov_area[0] = 5;
  ct_kcov_area[1] = 11; ct_kcov_area[2] = 11; ct_kcov_area[3] = 11;
  ct_kcov_area[4] = 12; ct_kcov_area[5] = 13;
  ct_kcov_active = 1;
  ct_kcov_current_call = 0;
  ct_kcov_call_count = 1;
  ct_kcov_calls[0].start = 0;
  ct_kcov_stop();
  assert(!ct_kcov_saturated);
  assert(ct_kcov_count == 3);
  assert(ct_kcov_raw_count == 5);
  assert(lseek(fileno(ct_kcov_spool), 0, SEEK_SET) == 0);
  assert(read(fileno(ct_kcov_spool), token, sizeof(token)) == (ssize_t)sizeof(token));
  assert(token[0] == 11 && token[1] == 3);
  assert(read(fileno(ct_kcov_spool), token, sizeof(token)) == (ssize_t)sizeof(token));
  assert(token[0] == 12 && token[1] == 1);
  assert(read(fileno(ct_kcov_spool), token, sizeof(token)) == (ssize_t)sizeof(token));
  assert(token[0] == 13 && token[1] == 1);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-pthread', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_runtime_feature_set_deduplicates_alternating_pc_pressure(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'kcov-feature-set.c'
            binary = pathlib.Path(temp) / 'kcov-feature-set'
            source.write_text(runtime + r'''
#include <assert.h>
int ioctl(int fd, unsigned long request, ...) { (void)fd; (void)request; return 0; }
int main(void)
{
  uint64_t feature[2];
  const size_t raw = 200000;
  ct_kcov_fd = 1;
  ct_kcov_spool = tmpfile();
  assert(ct_kcov_spool);
  ct_kcov_feature_set = 1;
  ct_kcov_entries = raw + 1;
  ct_kcov_area = calloc(ct_kcov_entries + 1, sizeof(uint64_t));
  assert(ct_kcov_area);
  ct_kcov_area[0] = raw;
  for (size_t i = 0; i < raw; i++) ct_kcov_area[1 + i] = (i & 1) ? 12 : 11;
  ct_kcov_active = 1;
  ct_kcov_current_call = 0;
  ct_kcov_call_count = 1;
  ct_kcov_calls[0].start = 0;
  ct_kcov_stop();
  assert(!ct_kcov_saturated);
  assert(ct_kcov_count == 4);
  assert(ct_kcov_raw_count == raw);
  assert(lseek(fileno(ct_kcov_spool), 0, SEEK_SET) == 0);
  assert(read(fileno(ct_kcov_spool), feature, sizeof(feature)) == (ssize_t)sizeof(feature));
  assert(feature[0] == 11 && feature[1] == 0);
  assert(read(fileno(ct_kcov_spool), feature, sizeof(feature)) == (ssize_t)sizeof(feature));
  assert(feature[0] == 12 && feature[1] == 0);
  assert(read(fileno(ct_kcov_spool), feature, sizeof(feature)) == (ssize_t)sizeof(feature));
  assert(feature[0] == 11 && feature[1] == 12);
  assert(read(fileno(ct_kcov_spool), feature, sizeof(feature)) == (ssize_t)sizeof(feature));
  assert(feature[0] == 12 && feature[1] == 11);
  assert(read(fileno(ct_kcov_spool), feature, sizeof(feature)) == 0);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-pthread', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_runtime_feature_table_limit_has_distinct_saturation_cause(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'kcov-feature-limit.c'
            binary = pathlib.Path(temp) / 'kcov-feature-limit'
            source.write_text(runtime + r'''
#include <assert.h>
int ioctl(int fd, unsigned long request, ...) { (void)fd; (void)request; return 0; }
int main(void)
{
  ct_kcov_spool = tmpfile();
  assert(ct_kcov_spool);
  ct_kcov_feature_table = calloc(CT_KCOV_FEATURE_TABLE_SIZE,
                                 sizeof(*ct_kcov_feature_table));
  assert(ct_kcov_feature_table);
  ct_kcov_feature_used = CT_KCOV_FEATURE_TABLE_SIZE - 1;
  assert(ct_kcov_append_feature(123, 0) < 0);
  assert(ct_kcov_saturated);
  assert(ct_kcov_saturation_reasons == CT_KCOV_FLAG_FEATURE_TABLE_LIMIT);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-pthread', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_guest_kcov_buffer_default_fits_runtime_and_parser_limits(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        guest = (ROOT / 'tests/guest-campaign.sh').read_text()
        feedback = (ROOT / 'scripts/kcov_feedback.py').read_text()
        maximum = int(re.search(r'#define CT_KCOV_MAX_ENTRIES (\d+)UL', runtime)[1])
        default = int(re.search(r'CT_KCOV_ENTRIES:-([0-9]+)', guest)[1])
        parser_maximum = int(re.search(r'count > ([0-9_]+)', feedback)[1].replace('_', ''))
        self.assertGreaterEqual(maximum, default)
        self.assertLessEqual(maximum, parser_maximum + 1)
        self.assertIn('CT_FALLOCATE_MAX_BYTES', guest)
        self.assertIn('CT_IO_MAX_BYTES', guest)

    def test_runtime_bounds_pathological_fallocate_length(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'fallocate-bound.c'
            binary = pathlib.Path(temp) / 'fallocate-bound'
            source.write_text(runtime + r'''
#include <assert.h>
int main(void)
{
  int adapted = 0;
  ct_fallocate_max_bytes = 64 * 1024 * 1024ULL;
  assert(ct_bound_fallocate_length(0x100000000L, &adapted) ==
         (long)(64 * 1024 * 1024ULL));
  assert(adapted == 1);
  adapted = 0;
  assert(ct_bound_fallocate_length(0x1000L, &adapted) == 0x1000L);
  assert(adapted == 0);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-pthread', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_runtime_bounds_pathological_io_length(self):
        runtime = (ROOT / 'tests/syz-runtime.h').read_text()
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'io-bound.c'
            binary = pathlib.Path(temp) / 'io-bound'
            source.write_text(runtime + r'''
#include <assert.h>
int main(void)
{
  int adapted = 0;
  ct_io_max_bytes = 1024 * 1024ULL;
  assert(ct_bound_io_length(0x100000000L, &adapted) ==
         (long)(1024 * 1024ULL));
  assert(adapted == 1);
  adapted = 0;
  assert(ct_bound_io_length(0x1000L, &adapted) == 0x1000L);
  assert(adapted == 0);
  return 0;
}
''')
            subprocess.run(['gcc', '-O0', '-pthread', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_mutation_and_changed_seed_preserve_exact_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = pathlib.Path(temp) / 'a', pathlib.Path(temp) / 'b'
            subprocess.run([str(GEN), '-out', str(a), '-seed', '7', '-calls', '16'], check=True)
            subprocess.run([str(GEN), '-out', str(b), '-seed', '8', '-calls', '16', '-mutate', str(a / 'program.syz')], check=True)
            meta = json.loads((b / 'program.json').read_text())
            self.assertEqual(meta['method'], 'Mutate')
            self.assertEqual(len(meta['calls']), 16)
            self.assertNotEqual((a / 'program.syz').read_bytes(), (b / 'program.syz').read_bytes())
            self.assertTrue(all(c['name'] in meta['allowlist'] for c in meta['calls']))

    def test_ordinary_generate_has_distinct_call_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp) / 'ordinary'
            subprocess.run([str(GEN), '-out', str(out), '-seed', '7001', '-calls', '16'], check=True)
            meta = json.loads((out / 'program.json').read_text())
            self.assertEqual(meta['method'], 'Generate')
            self.assertEqual(meta['call_origins'], ['fresh_generate'] * 16)

    def test_resource_valid_seed_keeps_contract_and_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp) / 'resource'
            subprocess.run([str(GEN), '-out', str(out), '-seed', '424242', '-calls', '24',
                            '-input-mode', 'resource-valid', '-resource-template', 'namespace'], check=True)
            meta = json.loads((out / 'program.json').read_text())
            self.assertEqual(meta['input_mode'], 'resource-valid')
            self.assertEqual(meta['resource_contract'], 'private-worker-tree-v2')
            self.assertFalse(meta['protected_prefix'])
            self.assertTrue(meta['resource_prefix_mutable'])
            self.assertEqual(meta['resource_template'], 'namespace')
            self.assertEqual(meta['method'], 'ResourceSeedMutate')
            self.assertEqual(meta['resource_seed_calls'], 15)
            self.assertLessEqual(meta['resource_prefix_calls'], 15)
            self.assertLessEqual(len(meta['calls']), 24)
            self.assertEqual(len(meta['calls']), 24)
            self.assertEqual(len(meta['call_origins']), len(meta['calls']))
            self.assertFalse(meta['namespace_parameter_adaptation'])
            self.assertTrue(meta['has_data_call'])
            self.assertTrue(any(origin == 'typed_mutation' for origin in meta['call_origins']))

    def test_resource_valid_parent_continues_with_typed_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            seed, child = root / 'seed', root / 'child'
            subprocess.run([str(GEN), '-out', str(seed), '-seed', '424243', '-calls', '24',
                            '-input-mode', 'resource-valid', '-resource-template', 'file-state'], check=True)
            subprocess.run([str(GEN), '-out', str(child), '-seed', '424244', '-calls', '24',
                            '-input-mode', 'resource-valid', '-resource-template', 'auto',
                            '-mutate', str(seed / 'program.syz')], check=True)
            meta = json.loads((child / 'program.json').read_text())
            self.assertEqual(meta['method'], 'ResourceMutate')
            self.assertEqual(meta['resource_template'], 'file-state')
            self.assertEqual(len(meta['calls']), 24)
            self.assertEqual(len(meta['call_origins']), 24)
            self.assertLess(meta['resource_prefix_calls'], 8)
            self.assertTrue(any(origin == 'typed_mutation' for origin in meta['call_origins']))
            self.assertNotEqual((seed / 'program.syz').read_bytes(),
                                (child / 'program.syz').read_bytes())
            self.assertTrue(all(call['name'] in meta['allowlist'] for call in meta['calls']))
            self.assertTrue(meta['has_data_call'])

    def test_shared_contention_seed_fallocate_targets_shared_source(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp) / 'shared-contention'
            subprocess.run([str(GEN), '-out', str(out), '-seed', '424242', '-calls', '16',
                            '-input-mode', 'resource-valid', '-resource-template', 'shared-contention'],
                           check=True)
            meta = json.loads((out / 'program.json').read_text())
            names = [call['name'] for call in meta['calls']]
            self.assertEqual(meta['resource_template'], 'shared-contention')
            self.assertEqual(meta['resource_seed_calls'], 16)
            self.assertLessEqual(meta['resource_prefix_calls'], 16)
            self.assertEqual(len(meta['call_origins']), 16)
            source = (out / 'program.syz').read_text()
            self.assertIn("'source\\x00'", source)
            self.assertIn('0x1000000', source)
            self.assertEqual(len(names), 16)

    def test_file_state_template_preserves_typed_fd_with_short_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp) / 'file-state'
            subprocess.run([str(GEN), '-out', str(out), '-seed', '424243', '-calls', '16',
                            '-input-mode', 'resource-valid', '-resource-template', 'file-state'], check=True)
            meta = json.loads((out / 'program.json').read_text())
            names = [call['name'] for call in meta['calls']]
            self.assertEqual(meta['resource_template'], 'file-state')
            self.assertEqual(meta['resource_seed_calls'], 8)
            self.assertLessEqual(meta['resource_prefix_calls'], 8)
            self.assertEqual(len(meta['call_origins']), 16)
            self.assertEqual(len(names), 16)
            self.assertTrue(meta['has_data_call'])

    def test_missing_duplicate_wrong_or_unverified_execution_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            programs = pathlib.Path(temp)
            (programs / 'b1-w0').mkdir()
            (programs / 'b1-w0/program.json').write_text(json.dumps({'calls':[{'index':0,'nr':1,'name':'write'}]}))
            call = dict(program='b1-w0', id='segment-000000-b1-100-1', call_index=0, nr=1, name='write', verified=True)
            self.assertEqual(execution_issues(programs,[call],'segment-000000',1,1), [])
            self.assertEqual(execution_issues(programs,[dict(call,call_index='0')],'segment-000000',1,1), [])
            for calls in ([], [call,call], [dict(call,verified=False)], [dict(call,nr=2)],
                          [call, dict(call,program='b9-w0')]):
                self.assertTrue(execution_issues(programs,calls,'segment-000000',1,1))

    def test_c_export_disagreement_is_rejected(self):
        source = 'int main(void) { syscall(__NR_write, 1, 2, 3); }'
        with self.assertRaises(ValueError):
            instrument(source, {'calls':[dict(name='read',native_name='read')]}, '')


if __name__ == '__main__':
    unittest.main()
