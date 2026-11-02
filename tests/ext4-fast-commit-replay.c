// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int write_file(const char *path, unsigned char tag, int blocks)
{
	unsigned char buf[4096];
	int fd, i;

	memset(buf, tag, sizeof(buf));
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
	if (fd < 0)
		return -1;
	for (i = 0; i < blocks; i++) {
		if (pwrite(fd, buf, sizeof(buf), (off_t)i * sizeof(buf)) !=
		    (ssize_t)sizeof(buf)) {
			close(fd);
			return -1;
		}
	}
	if (fsync(fd) || close(fd))
		return -1;
	return 0;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/fastcommit";
	char path[256];
	int i;

	if (mkdir(dir, 0700) && errno != EEXIST) {
		perror("mkdir");
		return 1;
	}
	for (i = 0; i < 128; i++) {
		snprintf(path, sizeof(path), "%s/fc-%03d", dir, i);
		if (write_file(path, (unsigned char)(i + 1), 8)) {
			perror("write_file");
			return 2;
		}
		if (i & 1) {
			if (unlink(path)) {
				perror("unlink");
				return 3;
			}
		}
	}
	printf("fast_commit_workload files=128 status=ready\n");
	return 0;
}
