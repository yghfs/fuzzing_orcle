// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mnt = argc > 1 ? argv[1] : "/mnt/ext4-orphan-shutdown";
	char path[256];
	unsigned int mode = EXT4_GOING_FLAGS_DEFAULT;
	char data[4096];
	int fd, mfd, rc, saved;

	memset(data, 'O', sizeof(data));
	if (mkdir(mnt, 0700) && errno != EEXIST)
		return 1;
	if (snprintf(path, sizeof(path), "%s/victim", mnt) >= (int)sizeof(path))
		return 1;
	fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return 1;
	if (write(fd, data, sizeof(data)) != (ssize_t)sizeof(data) || fsync(fd))
		return 1;
	if (unlink(path))
		return 1;
	if (syncfs(fd))
		return 1;
	mfd = open(mnt, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (mfd < 0)
		return 1;
	rc = ioctl(mfd, EXT4_IOC_SHUTDOWN, &mode);
	saved = errno;
	printf("shutdown_rc=%d shutdown_errno=%d (%s)\n", rc, rc ? saved : 0,
	       rc ? strerror(saved) : "ok");
	close(mfd);
	rc = close(fd);
	saved = errno;
	printf("close_rc=%d close_errno=%d (%s)\n", rc, rc ? saved : 0,
	       rc ? strerror(saved) : "ok");
	return 0;
}
