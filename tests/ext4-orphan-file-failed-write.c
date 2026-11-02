// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int fill_until_full(int fd)
{
	const off_t step = 64 * 1024;
	off_t size = 0;

	for (;;) {
		if (fallocate(fd, 0, size, step) == 0) {
			size += step;
			continue;
		}
		if (errno == ENOSPC || errno == EDQUOT)
			return 0;
		fprintf(stderr, "fill fallocate failed at %lld: %s\n",
			(long long)size, strerror(errno));
		return -1;
	}
}

int main(int argc, char **argv)
{
	char path[512], fill_path[512], block[4096];
	ssize_t written;
	int victim, filler;

	if (argc != 2)
		return 2;
	if (snprintf(path, sizeof(path), "%s/victim", argv[1]) >=
	    (int)sizeof(path) ||
	    snprintf(fill_path, sizeof(fill_path), "%s/filler", argv[1]) >=
	    (int)sizeof(fill_path))
		return 2;
	memset(block, 0x5a, sizeof(block));
	victim = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (victim < 0) {
		perror("open victim");
		return 1;
	}
	filler = open(fill_path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (filler < 0) {
		perror("open filler");
		return 1;
	}
	if (fill_until_full(filler))
		return 1;
	if (fsync(filler) < 0 && errno != ENOSPC)
		perror("fsync filler");

	errno = 0;
	written = pwrite(victim, block, sizeof(block), 0);
	printf("failed_write_rc=%lld errno=%d (%s)\n", (long long)written,
	       written < 0 ? errno : 0, written < 0 ? strerror(errno) : "ok");
	if (written >= 0)
		return 3;
	if (syncfs(victim) < 0)
		perror("syncfs");
	sleep(1);
	close(filler);
	close(victim);
	return 0;
}
