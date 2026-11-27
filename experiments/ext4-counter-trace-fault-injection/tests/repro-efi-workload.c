// SPDX-License-Identifier: GPL-2.0
/*
 * Deterministic single-syscall fixtures for the CSV error-site repro.
 *
 * Each mode performs exactly one metadata-mutating syscall against a mounted
 * ext4 image, wrapped in an EFI native-syscall window so the kernel-side
 * formula gate and per-call site occurrences are well defined. With EFI_PLAN
 * unset the identical run is the no-fault control.
 *
 *   group-extend MOUNT BLOCKS               EXT4_IOC_GROUP_EXTEND
 *   group-add MOUNT GROUP BB IB IT BLOCKS   EXT4_IOC_GROUP_ADD
 *   mkdir|rmdir|unlink|rename MOUNT
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#include "efi-runtime.h"

/* uapi/linux/ext4.h, inlined so the fixture has no kernel-header dependency */
#define EXT4_IOC_GROUP_EXTEND _IOW('f', 7, unsigned long)
#define EXT4_IOC_GROUP_ADD _IOW('f', 8, struct ext4_new_group_input)
struct ext4_new_group_input {
	uint32_t group;
	uint64_t block_bitmap;
	uint64_t inode_bitmap;
	uint64_t inode_table;
	uint32_t blocks_count;
	uint16_t reserved_blocks;
	uint16_t unused;
};

static const char *op_name;

/* Issue one syscall inside the EFI window and print the real return value. */
static void issue(unsigned index, long nr, long a, long b, long c, long d,
		  long e, long f, unsigned long long dev)
{
	long ret;
	int error;

	efi_call_begin(index, nr, dev);
	errno = 0;
	ret = syscall(nr, a, b, c, d, e, f);
	efi_call_end();
	error = ret == -1 ? errno : 0;
	printf("REPRO_RESULT op=%s ret=%ld errno=%d (%s)\n", op_name, ret, error,
	       error ? strerror(error) : "none");
	fflush(stdout);
}

/*
 * Make the target inode resident but drop its on-disk buffer.
 *
 * A blkdebug rule fails a block-layer read. ext4 only re-reads an inode-table
 * block when the buffer is gone, so with the block warm the injected error
 * never fires and the syscall silently succeeds. Loading the inode first keeps
 * it in the inode hash (lookup then does no I/O), and dropping the page cache
 * discards the buffer, so the mark_inode_dirty inside the syscall is the read
 * that fails. That is what turns a block-layer EIO into the specific error
 * exit rather than an earlier unrelated one.
 */
static void prime_and_drop(const char *path)
{
	struct stat st;
	int fd;

	if (path && stat(path, &st)) {
		perror("prime stat");
		exit(2);
	}
	fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);
	if (fd < 0) {
		perror("open drop_caches");
		exit(2);
	}
	if (write(fd, "1\n", 2) != 2) {
		perror("write drop_caches");
		exit(2);
	}
	close(fd);
}

int main(int argc, char **argv)
{
	const char *mode, *mount;
	char path[512], target[512];
	unsigned long blocks;
	unsigned long long dev;
	struct stat st;
	struct ext4_new_group_input input = { 0 };
	int fd;

	if (argc < 3) {
		fprintf(stderr, "usage: %s MODE MOUNT [args...]\n", argv[0]);
		return 2;
	}
	mode = argv[1];
	mount = argv[2];
	op_name = mode;
	if (chdir(mount)) {
		perror("chdir mount");
		return 2;
	}
	/* Resolve the device before the prime: after drop_caches a stat of the
	 * mount point would re-read the very itable block we just faulted. */
	if (stat(".", &st)) {
		perror("stat mount");
		return 2;
	}
	dev = (unsigned long long)st.st_dev;
	efi_init();

	if (!strcmp(mode, "group-extend")) {
		if (argc != 4)
			return 2;
		blocks = strtoul(argv[3], NULL, 0);
		fd = open(mount, O_RDONLY | O_DIRECTORY);
		if (fd < 0) {
			perror("open mount");
			return 2;
		}
		issue(0, __NR_ioctl, fd, EXT4_IOC_GROUP_EXTEND, (long)&blocks,
		      0, 0, 0, dev);
		close(fd);
	} else if (!strcmp(mode, "group-add")) {
		if (argc != 8)
			return 2;
		input.group = strtoul(argv[3], NULL, 0);
		input.block_bitmap = strtoull(argv[4], NULL, 0);
		input.inode_bitmap = strtoull(argv[5], NULL, 0);
		input.inode_table = strtoull(argv[6], NULL, 0);
		input.blocks_count = strtoul(argv[7], NULL, 0);
		fd = open(mount, O_RDONLY | O_DIRECTORY);
		if (fd < 0) {
			perror("open mount");
			return 2;
		}
		issue(0, __NR_ioctl, fd, EXT4_IOC_GROUP_ADD, (long)&input,
		      0, 0, 0, dev);
		close(fd);
	} else if (!strcmp(mode, "mkdir")) {
		snprintf(path, sizeof(path), "./child");
		prime_and_drop(NULL);
		issue(0, __NR_mkdirat, AT_FDCWD, (long)path, 0700, 0, 0, 0, dev);
	} else if (!strcmp(mode, "rmdir")) {
		snprintf(path, sizeof(path), "./child");
		prime_and_drop(path);
		issue(0, __NR_unlinkat, AT_FDCWD, (long)path, AT_REMOVEDIR,
		      0, 0, 0, dev);
	} else if (!strcmp(mode, "unlink")) {
		snprintf(path, sizeof(path), "./victim");
		prime_and_drop(path);
		issue(0, __NR_unlinkat, AT_FDCWD, (long)path, 0, 0, 0, 0, dev);
	} else if (!strcmp(mode, "rename")) {
		snprintf(path, sizeof(path), "./src");
		snprintf(target, sizeof(target), "./dst");
		prime_and_drop(path);
		issue(0, __NR_renameat2, AT_FDCWD, (long)path, AT_FDCWD,
		      (long)target, 0, 0, dev);
	} else {
		fprintf(stderr, "unknown mode: %s\n", mode);
		return 2;
	}

	efi_finish();
	return 0;
}
