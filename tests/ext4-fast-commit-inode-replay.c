// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define BLOCK_SIZE 4096
#define BLOCKS 64

static int seed_file(const char *dir)
{
	char path[512];
	char block[BLOCK_SIZE];
	int dirfd, fd, i;

	if (mkdir(dir, 0700) < 0)
		return -1;
	snprintf(path, sizeof(path), "%s/tracked", dir);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	memset(block, 'A', sizeof(block));
	for (i = 0; i < BLOCKS; i++) {
		if (pwrite(fd, block, sizeof(block), (off_t)i * BLOCK_SIZE) !=
		    (ssize_t)sizeof(block))
			goto fail;
	}
	if (fsync(fd) < 0)
		goto fail;
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		goto fail;
	if (fsync(dirfd) < 0 || syncfs(dirfd) < 0) {
		close(dirfd);
		goto fail;
	}
	close(dirfd);
	memset(block, 'B', sizeof(block));
	if (pwrite(fd, block, sizeof(block), 0) != (ssize_t)sizeof(block) ||
	    fsync(fd) < 0)
		goto fail;
	printf("fast_commit_ready blocks=%d\n", BLOCKS);
	fflush(stdout);
	for (;;)
		pause();
fail:
	{
		int saved = errno;
		close(fd);
		errno = saved;
	}
	return -1;
}

static int verify_file(const char *dir)
{
	char path[512];
	char byte;
	int fd;

	snprintf(path, sizeof(path), "%s/tracked", dir);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	if (pread(fd, &byte, 1, 0) != 1 || byte != 'B') {
		close(fd);
		errno = EIO;
		return -1;
	}
	close(fd);
	puts("fast_commit_verify=ok");
	return 0;
}

int main(int argc, char **argv)
{
	if (argc != 3 || (strcmp(argv[1], "seed") &&
			  strcmp(argv[1], "verify"))) {
		fprintf(stderr, "usage: %s seed|verify DIR\n", argv[0]);
		return 2;
	}
	if (!strcmp(argv[1], "seed")) {
		if (seed_file(argv[2]) < 0) {
			perror("fast-commit seed");
			return 1;
		}
	} else if (verify_file(argv[2]) < 0) {
		perror("fast-commit verify");
		return 1;
	}
	return 0;
}
