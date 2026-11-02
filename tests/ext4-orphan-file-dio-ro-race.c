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
	unsigned long long chunk = argc > 2 ? strtoull(argv[2], NULL, 0) :
							  4ULL * 1024 * 1024;
	unsigned long long total = argc > 3 ? strtoull(argv[3], NULL, 0) :
							  512ULL * 1024 * 1024;
		char path[512];
	void *buf;
	int fd;
	int result = 0;

	if (argc < 2 || !chunk || chunk % 4096 || !total || total % chunk) {
		fprintf(stderr, "usage: %s DIR [CHUNK_BYTES [TOTAL_BYTES]]\n", argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700) < 0 && errno != EEXIST) {
		perror("mkdir");
		return 1;
	}
	snprintf(path, sizeof(path), "%s/dio-file", argv[1]);
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_DIRECT | O_CLOEXEC, 0600);
	if (fd < 0) {
		perror("open");
		return 1;
	}
	if (posix_memalign(&buf, 4096, chunk)) {
		fprintf(stderr, "posix_memalign failed\n");
		return 1;
	}
	memset(buf, 0x5a, chunk);
	printf("dio_start pid=%ld chunk=%llu total=%llu\n", (long)getpid(),
	       chunk, total);
	fflush(stdout);
	for (unsigned long long off = 0; off < total; off += chunk) {
		ssize_t n = pwrite(fd, buf, chunk, off);
		if (n != (ssize_t)chunk) {
			result = n < 0 ? 1 : 0;
			printf("dio_stop offset=%llu rc=%zd errno=%d (%s)\n", off, n,
			       n < 0 ? errno : 0,
			       n < 0 ? strerror(errno) : "short write");
			break;
		}
		if ((off / chunk & 15) == 0)
			printf("dio_progress bytes=%llu\n", off + chunk);
	}
	fflush(stdout);
	fsync(fd);
	close(fd);
	free(buf);
	return result;
}
