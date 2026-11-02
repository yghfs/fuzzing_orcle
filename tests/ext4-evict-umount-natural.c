// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

/* Close the last fd of an unlinked file, then let the shell umount and
 * remount read-only.  The unlink is journaled before close so the final
 * eviction happens on a healthy, still-active mount without any shutdown
 * ioctl; if ext4 fails to free the inode there, the leaked bitmap bit is
 * visible after the fresh remount and in e2fsck. */
int main(int argc, char **argv)
{
	const char *mnt = argc > 1 ? argv[1] : "/mnt/ext4-evict-umount";
	char path[256], data[4096];
	int fd;

	if (snprintf(path, sizeof(path), "%s/victim", mnt) >= (int)sizeof(path))
		return 2;
	memset(data, 'V', sizeof(data));
	fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return 1;
	if (write(fd, data, sizeof(data)) != (ssize_t)sizeof(data) || fsync(fd)) {
		close(fd);
		return 1;
	}
	if (close(fd) || unlink(path))
		return 1;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 1;
	if (syncfs(fd) || close(fd))
		return 1;
	sync();
	return 0;
}
