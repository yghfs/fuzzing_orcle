// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ext4.h>
#include <linux/fiemap.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/statfs.h>
#include <unistd.h>

static void print_statfs(int fd, const char *label)
{
	struct statfs st;

	if (fstatfs(fd, &st) < 0) {
		perror("statfs");
		return;
	}
	printf("%s_blocks=%" PRIu64 " %s_bfree=%" PRIu64 "\n",
	       label, (uint64_t)st.f_blocks, label, (uint64_t)st.f_bfree);
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/";
	uint32_t group = argc > 2 ? strtoul(argv[2], NULL, 0) : 16;
	uint64_t start = argc > 3 ? strtoull(argv[3], NULL, 0) : 524288;
	uint32_t blocks = argc > 4 ? strtoul(argv[4], NULL, 0) : 32768;
	struct ext4_new_group_input in = {
		.group = group,
		.block_bitmap = start,
		.inode_bitmap = start + 1,
		.inode_table = start + 2,
		.blocks_count = blocks,
		.reserved_blocks = 0,
	};
	const off_t chunk = 16 * 1024 * 1024;
	const off_t post_add_len = 64 * 1024 * 1024;
	struct {
		struct fiemap map;
		struct fiemap_extent extents[64];
	} fm = {};
	off_t offset = 0;
	int fd, file_fd, rc, saved_errno, alloc_rc = 0;

	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	print_statfs(fd, "before");
	file_fd = openat(fd, "group-add-filler", O_CREAT | O_RDWR | O_CLOEXEC,
			 0600);
	if (file_fd < 0) {
		perror("open filler");
		return 1;
	}
	while (offset < (off_t)8 * 1024 * 1024 * 1024) {
		errno = 0;
		if (fallocate(file_fd, 0, offset, chunk) < 0) {
			if (errno == ENOSPC)
				break;
			perror("fallocate filler");
			close(file_fd);
			close(fd);
			return 1;
		}
		offset += chunk;
	}
	printf("fill_end=%" PRIu64 " fill_errno=%d (%s)\n",
	       (uint64_t)offset, errno, strerror(errno));
	errno = 0;
	rc = ioctl(fd, EXT4_IOC_GROUP_ADD, &in);
	saved_errno = errno;
	printf("group=%" PRIu32 " start=%" PRIu64 " blocks=%" PRIu32
	       " ioctl_rc=%d errno=%d (%s)\n", group, start, blocks, rc,
	       saved_errno, strerror(saved_errno));
	errno = 0;
	alloc_rc = fallocate(file_fd, 0, offset, post_add_len);
	saved_errno = errno;
	printf("post_add_fallocate_rc=%d errno=%d (%s)\n", alloc_rc,
	       saved_errno, strerror(saved_errno));
	if (alloc_rc == 0) {
		if (fsync(file_fd) < 0)
			perror("fsync filler");
		fm.map.fm_start = offset;
		fm.map.fm_length = post_add_len;
		fm.map.fm_extent_count = 64;
		if (ioctl(file_fd, FS_IOC_FIEMAP, &fm.map) < 0) {
			perror("FIEMAP filler");
		} else {
			for (uint32_t i = 0; i < fm.map.fm_mapped_extents; i++) {
				uint64_t pblk = fm.extents[i].fe_physical >> 12;
				printf("extent%u_logical=%" PRIu64 " pblk=%" PRIu64
				       " length=%" PRIu64 " flags=%u\n", i,
				       (uint64_t)fm.extents[i].fe_logical, pblk,
				       (uint64_t)fm.extents[i].fe_length,
				       fm.extents[i].fe_flags);
			}
		}
	}
	print_statfs(fd, "after");
	close(file_fd);
	close(fd);
	return alloc_rc < 0 ? 3 : 0;
}
