// SPDX-License-Identifier: GPL-2.0-only
/* Aggressive natural extent-tree growth with continuous memory pressure */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define BLOCK_SIZE 4096
#define EXTENT_SIZE (BLOCK_SIZE * 8)
#define MAX_ATTEMPTS 50

static volatile int keep_pressure = 1;

static void *pressure_thread(void *arg)
{
	size_t pressure_mb = *(size_t *)arg;
	size_t pressure_bytes = pressure_mb * 1024 * 1024;
	void *p;

	/* Allocate and touch memory repeatedly to maintain pressure */
	while (keep_pressure) {
		p = malloc(pressure_bytes);
		if (!p)
			continue;

		/* Touch every page to bring into memory */
		memset(p, 0xa5, pressure_bytes);

		/* Hold for a moment */
		usleep(10000);

		free(p);
		usleep(5000);
	}

	return NULL;
}

static int extent_grow_attempt(const char *path, unsigned int attempt)
{
	char data[BLOCK_SIZE];
	int fd, rc;
	off_t offset;

	memset(data, 0x5a + attempt, sizeof(data));

	/* Create fresh file for each attempt */
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0) {
		perror("open");
		return -1;
	}

	/* Seed with 4 extents to fill root (eh_max == 4) */
	for (int i = 0; i < 4; i++) {
		offset = (off_t)i * 2 * BLOCK_SIZE;
		if (pwrite(fd, data, sizeof(data), offset) != sizeof(data)) {
			perror("pwrite seed");
			close(fd);
			return -1;
		}
		if (fsync(fd)) {
			perror("fsync seed");
			close(fd);
			return -1;
		}
	}

	/* Now trigger extent tree growth: allocate many more extents
	 * Each fallocate will force grow_indepth when root is full */
	for (int i = 0; i < 16; i++) {
		offset = (off_t)(4 + i) * 2 * BLOCK_SIZE;
		errno = 0;
		rc = fallocate(fd, 0, offset, BLOCK_SIZE);
		int fallocate_err = errno;

		if (fsync(fd)) {
			perror("fsync after fallocate");
			close(fd);
			return -1;
		}

		printf("attempt=%u grow_round=%d fallocate_rc=%d errno=%d\n",
		       attempt, i, rc, fallocate_err);
		fflush(stdout);

		if (rc < 0 && fallocate_err == ENOMEM) {
			/* This is what we're looking for - sb_getblk_gfp failed
			 * under memory pressure */
			printf("attempt=%u Got ENOMEM at grow_round=%d\n",
			       attempt, i);
			close(fd);
			return 0;
		}

		/* Small delay to let memory pressure build */
		usleep(100000);
	}

	close(fd);
	return 0;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-test";
	size_t pressure_mb = argc > 2 ? strtoul(argv[2], NULL, 0) : 256;
	unsigned int attempts = argc > 3 ? strtoul(argv[3], NULL, 0) : 50;

	pthread_t tid;
	char path[512];
	unsigned int ok = 0, other = 0;

	if (mkdir(dir, 0700) && errno != EEXIST) {
		perror("mkdir workdir");
		return 1;
	}

	printf("Starting memory pressure thread (pressure_mb=%zu)\n", pressure_mb);
	if (pthread_create(&tid, NULL, pressure_thread, &pressure_mb)) {
		perror("pthread_create");
		return 1;
	}

	/* Give pressure thread time to build up pressure */
	sleep(2);

	printf("Starting %u extent grow attempts\n", attempts);

	for (unsigned int i = 0; i < attempts; i++) {
		snprintf(path, sizeof(path), "%s/extent-%u", dir, i);
		if (extent_grow_attempt(path, i) == 0)
			ok++;
		else
			other++;

		if (unlink(path))
			perror("unlink");

		usleep(500000);
	}

	keep_pressure = 0;
	pthread_join(tid, NULL);

	printf("Results: ok=%u other=%u\n", ok, other);
	if (rmdir(dir))
		perror("rmdir");

	return other > 0 ? 1 : 0;
}
