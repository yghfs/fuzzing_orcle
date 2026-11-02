// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	char path[512];
	unsigned int blocks = argc > 2 ? strtoul(argv[2], NULL, 0) : 131072;
	char *buf;
	int fd;

	if (argc < 2 || !blocks) {
		fprintf(stderr, "usage: %s DIR [BLOCKS]\n", argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700) < 0 && errno != EEXIST) {
		perror("mkdir");
		return 1;
	}
	snprintf(path, sizeof(path), "%s/race-file", argv[1]);
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0) {
		perror("open");
		return 1;
	}
	buf = aligned_alloc(4096, 4096);
	if (!buf) {
		perror("aligned_alloc");
		return 1;
	}
	memset(buf, 0xa5, 4096);
	printf("writer_ready pid=%ld blocks=%u\n", (long)getpid(), blocks);
	fflush(stdout);
	for (unsigned int i = 0; i < blocks; i++) {
		ssize_t n = pwrite(fd, buf, 4096, (off_t)i * 4096);

		if (n != 4096) {
			printf("pwrite_stop block=%u rc=%zd errno=%d (%s)\n", i, n,
			       errno, strerror(errno));
			break;
		}
		if ((i & 1023) == 0)
			fsync(fd);
	}
	printf("writer_done\n");
	fsync(fd);
	close(fd);
	free(buf);
	return 0;
}
