// SPDX-License-Identifier: GPL-2.0-only
/* Deterministic syscall pressure on a fragmented ext4 extent tree. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef EXTENTS
#define EXTENTS 384
#endif
#ifndef STRIDE
#define STRIDE (16 * 1024)
#endif
#define BLOCK 4096
#define FILE_SIZE ((off_t)EXTENTS * STRIDE)

static int fd;
static pthread_barrier_t start_barrier;
static int cycles;

static void *worker(void *arg)
{
	uintptr_t id = (uintptr_t)arg;
	int i;

	pthread_barrier_wait(&start_barrier);
	for (i = 0; i < cycles; i++) {
		off_t off = (off_t)((i * 31 + id * 13) % (EXTENTS - 16)) * STRIDE;
		int ret;

		switch (id) {
		case 0:
			ret = fallocate(fd, FALLOC_FL_KEEP_SIZE |
					FALLOC_FL_PUNCH_HOLE, off, 8 * STRIDE);
			break;
		case 1:
			ret = fallocate(fd, FALLOC_FL_COLLAPSE_RANGE,
					4 * STRIDE, STRIDE);
			break;
		case 2:
			ret = fallocate(fd, FALLOC_FL_INSERT_RANGE,
					8 * STRIDE, STRIDE);
			break;
		default:
			ret = ftruncate(fd, FILE_SIZE - (i % 32) * STRIDE);
			if (!ret)
				ret = ftruncate(fd, FILE_SIZE);
			break;
		}
		if (ret && errno != EINVAL && errno != ENOSPC && errno != EFBIG)
			fprintf(stderr, "worker %lu iteration %d: %s\n",
				(unsigned long)id, i, strerror(errno));
		if (!(i % 8))
			fsync(fd);
	}
	return NULL;
}

int main(int argc, char **argv)
{
	char data[BLOCK];
	pthread_t threads[4];
	int i;

	cycles = argc > 2 ? atoi(argv[2]) : 100;
	if (argc < 2 || cycles < 1 || cycles > 10000) {
		fprintf(stderr, "usage: %s FILE [cycles 1..10000]\n", argv[0]);
		return 2;
	}
	fd = open(argv[1], O_CREAT | O_TRUNC | O_RDWR, 0600);
	if (fd < 0) {
		perror("open");
		return 1;
	}
	memset(data, 'X', sizeof(data));
	for (i = 0; i < EXTENTS; i++) {
		if (pwrite(fd, data, sizeof(data), (off_t)i * STRIDE) !=
		    sizeof(data)) {
			perror("pwrite");
			return 1;
		}
		/* Force physical allocation in batches while retaining gaps. */
		if (i % 16 == 15 && fsync(fd)) {
			perror("fsync");
			return 1;
		}
	}
	printf("seeded=%d stride=%d bytes=%lld\n", EXTENTS, STRIDE,
		(long long)FILE_SIZE);
	fflush(stdout);
	if (pthread_barrier_init(&start_barrier, NULL, 4))
		return 1;
	for (i = 0; i < 4; i++) {
		if (pthread_create(&threads[i], NULL, worker,
				   (void *)(uintptr_t)i))
			return 1;
	}
	for (i = 0; i < 4; i++)
		pthread_join(threads[i], NULL);
	fsync(fd);
	close(fd);
	printf("finished cycles=%d\n", cycles);
	return 0;
}
