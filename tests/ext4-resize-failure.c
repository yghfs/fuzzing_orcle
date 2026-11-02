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
#include <sys/statfs.h>
#include <unistd.h>

static int print_statfs(int fd, const char *label)
{
	struct statfs st;

	if (fstatfs(fd, &st) < 0)
		return -1;
	printf("%s_blocks=%" PRIu64 " %s_bfree=%" PRIu64
	       " %s_bavail=%" PRIu64 "\n",
	       label, (uint64_t)st.f_blocks, label, (uint64_t)st.f_bfree,
	       label, (uint64_t)st.f_bavail);
	return 0;
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/ext4-resize";
	uint64_t blocks = argc > 2 ? strtoull(argv[2], NULL, 0) : 505000;
	int fd, rc, ioctl_errno, sync_rc, sync_errno;

	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	if (print_statfs(fd, "before") < 0) {
		perror("statfs before resize");
		return 1;
	}
	errno = 0;
	rc = ioctl(fd, EXT4_IOC_RESIZE_FS, &blocks);
	ioctl_errno = errno;
	printf("resize_blocks=%" PRIu64 " rc=%d errno=%d (%s)\n",
	       blocks, rc, ioctl_errno, strerror(ioctl_errno));
	errno = 0;
	sync_rc = syncfs(fd);
	sync_errno = errno;
	printf("syncfs_rc=%d errno=%d (%s)\n", sync_rc, sync_errno,
	       strerror(sync_errno));
	if (print_statfs(fd, "after") < 0) {
		perror("statfs after resize");
		return 1;
	}
	close(fd);
	return 0;
}
