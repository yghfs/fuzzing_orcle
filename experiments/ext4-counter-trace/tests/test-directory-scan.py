#!/usr/bin/env python3
"""Run the exported directory scanner against packed ext4 dirent blocks."""
import pathlib
import subprocess
import tempfile
import unittest


PROJECT = pathlib.Path(__file__).resolve().parents[1]


def exported_source(filename):
    patch = (PROJECT / 'patches/0001-counter-trace-core-and-probes.patch').read_text()
    section = patch.split('+++ b/fs/ext4/' + filename + '\n', 1)[1].split('\n--- ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+'))


class DirectoryScanTests(unittest.TestCase):
    def test_packed_entries_and_malformed_headers(self):
        source = exported_source('counter_trace.c')
        start = source.index('static int ct_ns_count_dirents(')
        scanner = source[start:source.index('\n}\n', start) + 3]
        prelude = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
typedef uint32_t ext4_lblk_t;
struct ext4_dir_entry_2 {
  uint32_t inode;
  uint16_t rec_len;
  uint8_t name_len, file_type;
  char name[255];
};
struct ext4_dir_entry_tail { char data[12]; };
struct super_block { unsigned int s_blocksize, s_blocksize_bits; };
struct inode { struct super_block *i_sb; unsigned long size; };
struct ct_ns { u32 max_ino; unsigned int *dirents; };
struct dir_context { int actor; };
struct ct_inline_context { struct dir_context ctx; struct ct_ns *ns; };
struct buffer_head { char *b_data; };
static char block_data[4096];
static struct buffer_head buffer = { block_data };
static const int ct_inline_actor = 0;
#define EFSCORRUPTED EUCLEAN
#define ext4_has_metadata_csum(sb) 1
#define i_size_read(dir) ((dir)->size)
#define ext4_has_inline_data(dir) 0
#define ext4_ct_inline_dir(dir, ctx) (-EOPNOTSUPP)
#define ext4_bread(handle, dir, block, flags) (&buffer)
#define IS_ERR(bh) 0
#define PTR_ERR(bh) (-EIO)
#define ext4_rec_len_from_disk(value, size) (value)
#define ext4_dir_rec_len(len, dir) (((8 + (len)) + 3) & ~3U)
#define le32_to_cpu(value) (value)
#define brelse(bh) ((void)0)
#define cond_resched() ((void)0)
'''
        main = r'''
int main(void)
{
  struct super_block sb = {4096, 12};
  struct inode dir = {&sb, 4096};
  unsigned int refs[513] = {0};
  struct ct_ns ns = {512, refs};
  unsigned int offset = 0;
  for (unsigned int index = 0; index < 340; index++) {
    struct ext4_dir_entry_2 *de = (void *)(block_data + offset);
    de->inode = index + 1;
    de->rec_len = index == 339 ? 16 : 12;
    de->name_len = 1;
    de->name[0] = 'a';
    offset += de->rec_len;
  }
  assert(offset == 4096 - sizeof(struct ext4_dir_entry_tail));
  assert(ct_ns_count_dirents(&dir, &ns) == 0);
  for (unsigned int index = 1; index <= 340; index++)
    assert(refs[index] == 1);
  struct ext4_dir_entry_2 *first = (void *)block_data;
  first->name_len = 200;
  assert(ct_ns_count_dirents(&dir, &ns) == -EFSCORRUPTED);
  first->name_len = 1;
  first->rec_len = 10;
  assert(ct_ns_count_dirents(&dir, &ns) == -EFSCORRUPTED);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / 'directory-scan.c'
            binary = pathlib.Path(temp) / 'directory-scan'
            source.write_text(prelude + scanner + main)
            subprocess.run(['gcc', '-O2', '-o', str(binary), str(source)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


class ReplayHighwaterTests(unittest.TestCase):
    def test_replay_does_not_read_uninitialized_group_info(self):
        source = exported_source('counter_relations.c')
        start = source.index('void ext4_ct_ih(')
        function = source[start:source.index('\n}\n', start) + 3]
        prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
typedef uint32_t u32;
typedef int64_t s64;
struct sbi { unsigned int s_mount_state; };
struct super_block { unsigned int s_blocksize; struct sbi *sbi; };
struct ext4_group_desc { unsigned int bg_flags; };
struct buffer_head { unsigned char *b_data; };
static unsigned int busy_calls, records;
#define EXT4_FC_REPLAY 1
#define EXT4_BG_INODE_UNINIT 1
#define EXT4_CT_UNVERIFIED 3
#define EXT4_SB(sb) ((sb)->sbi)
#define EXT4_INODES_PER_GROUP(sb) 16
#define ext4_has_group_desc_csum(sb) true
#define cpu_to_le16(value) (value)
#define buffer_uptodate(bitmap) true
#define ext4_itable_unused_count(sb, gd) 0
#define ext4_test_bit(bit, data) 0
#define ct_status(valid, actual, expected) ((valid) ? 1 : 3)
#define ext4_ct_record(...) ((void)++records)
static bool ext4_checker_inode_bitmap_busy(struct super_block *sb, u32 group)
{
  assert(!(sb->sbi->s_mount_state & EXT4_FC_REPLAY));
  ++busy_calls;
  return false;
}
'''
        main = r'''
int main(void)
{
  struct sbi sbi = { EXT4_FC_REPLAY };
  struct super_block sb = { 4096, &sbi };
  struct ext4_group_desc gd = {0};
  unsigned char bits[2] = {0};
  struct buffer_head bitmap = {bits};
  ext4_ct_ih(&sb, 0, &gd, &bitmap, "replay");
  assert(busy_calls == 0 && records == 1);
  sbi.s_mount_state = 0;
  ext4_ct_ih(&sb, 0, &gd, &bitmap, "runtime");
  assert(busy_calls == 1 && records == 2);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'highwater.c'
            binary = pathlib.Path(temp) / 'highwater'
            path.write_text(prelude + function + main)
            subprocess.run(['gcc', '-O2', '-o', str(binary), str(path)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
