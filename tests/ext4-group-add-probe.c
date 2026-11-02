#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ext4.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/";
	uint32_t blocks = argc > 2 ? (uint32_t)strtoull(argv[2], NULL, 0) : 0;
	uint32_t group = argc > 3 ? (uint32_t)strtoul(argv[3], NULL, 0) : 16;
	struct ext4_new_group_input in = {
		.group = group,
		.block_bitmap = UINT64_MAX,
		.inode_bitmap = UINT64_MAX - 1,
		.inode_table = UINT64_MAX - 512,
		.blocks_count = blocks,
		.reserved_blocks = 0,
	};
	int fd, rc;

	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	rc = ioctl(fd, EXT4_IOC_GROUP_ADD, &in);
	printf("group=%" PRIu32 " blocks=%" PRIu32 " rc=%d errno=%d (%s) free_clusters=%" PRIu32 "\n",
	       group, blocks, rc, errno, strerror(errno), in.reserved_blocks);
	close(fd);
	return rc < 0 ? 2 : 0;
}
