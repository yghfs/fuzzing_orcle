// SPDX-License-Identifier: GPL-2.0-only
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
	uint64_t blocks = argc > 2 ? strtoull(argv[2], NULL, 0) : 786432;
	int fd, rc;

	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	rc = ioctl(fd, EXT4_IOC_RESIZE_FS, &blocks);
	printf("resize_blocks=%" PRIu64 " rc=%d errno=%d (%s)\n",
	       blocks, rc, errno, strerror(errno));
	close(fd);
	return rc < 0 ? 2 : 0;
}
