// SPDX-License-Identifier: GPL-2.0-only
/* Precise extent-tree growth failure with i_blocks persistence */
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

static volatile int keep_pressure = 1;

static void *pressure_thread(void *arg)
{
	size_t pressure_mb = *(size_t *)arg;
	size_t pressure_bytes = pressure_mb * 1024 * 1024;
	void *p;

	while (keep_pressure) {
		p = malloc(pressure_bytes);
		if (!p)
			continue;
		memset(p, 0xa5, pressure_bytes);
		usleep(10000);
		free(p);
		usleep(5000);
	}

	return NULL;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-test";
	size_t pressure_mb = argc > 2 ? strtoul(argv[2], NULL, 0) : 256;
	unsigned int attempts = argc > 3 ? strtoul(argv[3], NULL, 0) : 20;

	pthread_t tid;
	char path[512];
	int fd;
	char data[BLOCK_SIZE];
	off_t offset;

	if (mkdir(dir, 0700) && errno != EEXIST) {
		perror("mkdir workdir");
		return 1;
	}

	printf("Starting memory pressure thread (pressure_mb=%zu)\n", pressure_mb);
	if (pthread_create(&tid, NULL, pressure_thread, &pressure_mb)) {
		perror("pthread_create");
		return 1;
	}

	sleep(2);

	printf("Starting %u extent grow attempts (NO UNLINK - keep files)\n", attempts);

	memset(data, 0x5a, sizeof(data));

	/* Create files WITHOUT deleting them - they stay in the filesystem
	 * so their i_blocks errors are persisted to disk */
	for (unsigned int i = 0; i < attempts; i++) {
		snprintf(path, sizeof(path), "%s/extent-%u", dir, i);

		/* Fresh file for each attempt */
		fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			perror("open");
			continue;
		}

		/* Seed with 4 extents to fill root (eh_max == 4) */
		for (int j = 0; j < 4; j++) {
			offset = (off_t)j * 2 * BLOCK_SIZE;
			if (pwrite(fd, data, sizeof(data), offset) != sizeof(data)) {
				perror("pwrite seed");
				close(fd);
				goto next_attempt;
			}
			if (fsync(fd)) {
				perror("fsync seed");
				close(fd);
				goto next_attempt;
			}
		}

		/* Trigger extent tree growth - multiple allocations
		 * Each fallocate triggers grow_indepth when root is full
		 * Under memory pressure, sb_getblk_gfp may fail */
		for (int j = 0; j < 20; j++) {
			offset = (off_t)(4 + j) * 2 * BLOCK_SIZE;
			errno = 0;
			int rc = fallocate(fd, 0, offset, BLOCK_SIZE);
			int err = errno;

			if (fsync(fd)) {
				perror("fsync after fallocate");
			}

			printf("attempt=%u grow_round=%d fallocate_rc=%d errno=%d\n",
			       i, j, rc, err);
			fflush(stdout);

			if (rc < 0 && err == ENOMEM) {
				printf("attempt=%u Got ENOMEM at grow_round=%d - i_blocks will be wrong\n",
				       i, j);
				/* CRITICAL: Do NOT close/unlink - let this inode keep the
				 * leaked i_blocks value so it persists to disk */
				break;
			}

			usleep(100000);
		}

		/* Final fsync to ensure i_blocks is written to journal */
		fsync(fd);
		close(fd);

next_attempt:
		usleep(500000);
	}

	printf("Stopping memory pressure thread\n");
	keep_pressure = 0;
	pthread_join(tid, NULL);

	printf("Files remain in %s - they will be checked offline\n", dir);
	printf("Do NOT rmdir - we need these files for fsck verification\n");

	return 0;
}
