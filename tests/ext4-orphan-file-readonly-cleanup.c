// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	char path[512];
	int fd;

	if (argc != 2 && argc != 4) {
		fprintf(stderr, "usage: %s PATH [READY RELEASE]\n", argv[0]);
		return 2;
	}
	snprintf(path, sizeof(path), "%s/orphan-readonly", argv[1]);
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0) {
		perror("open");
		return 1;
	}
	if (fallocate(fd, 0, 0, 1024 * 1024) < 0) {
		perror("fallocate");
		return 1;
	}
	if (fsync(fd) < 0) {
		perror("fsync");
		return 1;
	}
	if (unlink(path) < 0) {
		perror("unlink");
		return 1;
	}
	printf("orphan_added path=%s\n", path);
	fflush(stdout);
	if (argc == 4) {
		int ready = open(argv[2], O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC,
				 0600);

		if (ready < 0 || write(ready, "1", 1) != 1) {
			perror("ready");
			return 1;
		}
		close(ready);
		while (access(argv[3], F_OK) < 0)
			sleep(1);
	}
	if (close(fd) < 0) {
		printf("close_errno=%d (%s)\n", errno, strerror(errno));
		return 1;
	}
	puts("orphan_close=ok");
	return 0;
}
