// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/ioctl.h>
#include <linux/ext4.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mnt = argc > 1 ? argv[1] : "/mnt/ext4-evict";
	char path[256], data[4096];
	int fd, mfd, rc, saved;
	unsigned int shutdown_mode = EXT4_GOING_FLAGS_DEFAULT;

	if (snprintf(path, sizeof(path), "%s/victim", mnt) >= (int)sizeof(path))
		return 2;
	memset(data, 'V', sizeof(data));
	fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0) return 1;
	if (write(fd, data, sizeof(data)) != (ssize_t)sizeof(data) || fsync(fd)) {
		close(fd);
		return 1;
	}
	if (close(fd))
		return 1;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		perror("reopen");
		return 1;
	}
	if (unlink(path)) {
		close(fd);
		return 1;
	}
	if (syncfs(fd)) {
		close(fd);
		return 1;
	}
	mfd = open(mnt, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (mfd < 0) {
		close(fd);
		return 1;
	}
	rc = ioctl(mfd, EXT4_IOC_SHUTDOWN, &shutdown_mode);
	saved = errno;
	printf("shutdown_rc=%d shutdown_errno=%d (%s)\n", rc, rc ? saved : 0,
	       rc ? strerror(saved) : "ok");
	if (rc) {
		close(mfd);
		close(fd);
		return 1;
	}
	close(mfd);
	rc = close(fd);
	saved = errno;
	printf("close_rc=%d close_errno=%d (%s)\n", rc, rc ? saved : 0,
	       rc ? strerror(saved) : "ok");
	return 0;
}
