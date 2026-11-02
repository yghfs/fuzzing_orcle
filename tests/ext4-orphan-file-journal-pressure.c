// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static int make_fragmented(const char *path, unsigned int extents,
				   unsigned int stride)
{
	int fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	char data[4096];

	if (fd < 0)
		return -1;
	memset(data, 0x5a, sizeof(data));
	for (unsigned int i = 0; i < extents; i++) {
		off_t off = (off_t)i * stride * sizeof(data);

		if (pwrite(fd, data, sizeof(data), off) != (ssize_t)sizeof(data)) {
			close(fd);
			return -1;
		}
	}
	if (fsync(fd) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

int main(int argc, char **argv)
{
	const char *dir;
	unsigned int extents = argc > 2 ? strtoul(argv[2], NULL, 0) : 4096;
	unsigned int rounds = argc > 3 ? strtoul(argv[3], NULL, 0) : 8;
	unsigned int stride = argc > 4 ? strtoul(argv[4], NULL, 0) : 3;
	char path[512];
	unsigned int ok = 0, failures = 0;

	if (argc < 2 || !extents || !rounds || !stride) {
		fprintf(stderr, "usage: %s DIR [EXTENTS [ROUNDS [STRIDE]]]\n", argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700) < 0 && errno != EEXIST)
		die("mkdir");
	dir = argv[1];
	for (unsigned int i = 0; i < rounds; i++) {
		int fd;
		int rc;

		snprintf(path, sizeof(path), "%s/f-%u", dir, i);
		fd = make_fragmented(path, extents, stride);
		if (fd < 0)
			die("make fragmented");
		printf("prepared round=%u fd=%d extents=%u stride=%u\n",
		       i, fd, extents, stride);
		if (unlink(path) < 0)
			die("unlink");
		rc = ftruncate(fd, 0);
		printf("ftruncate round=%u rc=%d errno=%d\n", i, rc,
		       rc < 0 ? errno : 0);
		if (rc < 0)
			failures++;
		if (close(fd) < 0)
			die("close");
		ok++;
	}
	printf("orphan_journal_pressure complete=%u failures=%u\n", ok,
	       failures);
	return 0;
}
