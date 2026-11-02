// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/mount.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mnt = argc > 1 ? argv[1] : "/mnt/ext4-evict-natural";
	char path[256], data[4096];
	int fd, mfd, rc, saved;

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
	if (close(fd))
		return 1;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 1;
	if (unlink(path)) {
		close(fd);
		return 1;
	}
	if (syncfs(fd)) {
		close(fd);
		return 1;
	}
	sync();
	sleep(2);
	if (syncfs(fd)) {
		close(fd);
		return 1;
	}
	sync();
	sleep(2);
	if (syncfs(fd)) {
		close(fd);
		return 1;
	}
	sync();
	sleep(2);
	mfd = open(mnt, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (mfd < 0) {
		close(fd);
		return 1;
	}
	errno = 0;
	rc = mount("/dev/vdb", mnt, "ext4", MS_REMOUNT | MS_RDONLY, NULL);
	saved = errno;
	printf("remount_rc=%d remount_errno=%d (%s)\n", rc,
	       rc ? saved : 0, rc ? strerror(saved) : "ok");
	if (rc) {
		char cmd[512];

		close(mfd);
		snprintf(cmd, sizeof(cmd),
			 "for p in /proc/[0-9]*; do for f in $p/fd/*; do "
			 "t=$(readlink $f 2>/dev/null); case \"$t\" in "
			 "\"%s\"/*) echo holder=$p $f $t;; esac; done; done",
			 mnt);
		if (system(cmd) < 0)
			perror("system");
	}
	rc = close(fd);
	saved = errno;
	printf("close_rc=%d close_errno=%d (%s)\n", rc,
	       rc ? saved : 0, rc ? strerror(saved) : "ok");
	return 0;
}
