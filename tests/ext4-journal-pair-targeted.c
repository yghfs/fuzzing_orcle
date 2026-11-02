// SPDX-License-Identifier: GPL-2.0-only
/* Isolate truncate, hole-punch and checkpoint journal edges. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define BLOCK 4096
#define BLOCKS 256
#define STRIDE 8

static void report_error(const char *op, int rc)
{
	if (rc < 0 && errno != EBUSY && errno != EINVAL && errno != EINTR &&
	    errno != ENOSPC && errno != EOPNOTSUPP && errno != ENOTTY)
		fprintf(stderr, "%s: %s\n", op, strerror(errno));
}

static int seed(int fd)
{
	char buf[BLOCK];
	int i;

	memset(buf, 'T', sizeof(buf));
	for (i = 0; i < BLOCKS; i++)
		if (pwrite(fd, buf, sizeof(buf), (off_t)i * STRIDE * BLOCK) !=
		    (ssize_t)sizeof(buf))
			return -1;
	return fsync(fd);
}

static void do_punch(int fd, int i)
{
	off_t off = (off_t)((i * 17) % (BLOCKS - 16)) * STRIDE * BLOCK;
	int rc = fallocate(fd, FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE,
			   off, 4 * STRIDE * BLOCK);
	report_error("punch", rc);
}

static void do_truncate(int fd, int i)
{
	off_t size = (off_t)(32 + (i % 160)) * STRIDE * BLOCK;
	int rc = ftruncate(fd, size);
	report_error("truncate_down", rc);
	if (!(i & 3)) {
		char buf[BLOCK];
		memset(buf, 'R', sizeof(buf));
		if (pwrite(fd, buf, sizeof(buf), size + BLOCK) !=
		    (ssize_t)sizeof(buf))
			perror("pwrite_extend");
	}
}

static void do_checkpoint(int fd, int i)
{
	unsigned int flags[] = { 0, EXT4_IOC_CHECKPOINT_FLAG_DISCARD,
		EXT4_IOC_CHECKPOINT_FLAG_ZEROOUT, EXT4_IOC_CHECKPOINT_FLAG_DRY_RUN };
	unsigned int arg = flags[i & 3];
	int rc = ioctl(fd, EXT4_IOC_CHECKPOINT, &arg);
	report_error("checkpoint", rc);
	if (fsync(fd) < 0)
		perror("fsync");
}

struct mixed_args {
	int fd;
	int id;
	int cycles;
	const char *ops;
};

static pthread_barrier_t mixed_barrier;

static void *mixed_worker(void *opaque)
{
	struct mixed_args *arg = opaque;
	int i;

	pthread_barrier_wait(&mixed_barrier);
	for (i = 0; i < arg->cycles; i++) {
		switch (arg->ops[arg->id]) {
		case 'p':
			do_punch(arg->fd, i);
			break;
		case 't':
			do_truncate(arg->fd, i);
			break;
		case 'c':
			do_checkpoint(arg->fd, i);
			break;
		case 'f':
			if (fsync(arg->fd) < 0)
				perror("mixed_fsync");
			break;
		}
		if (!(i & 31) && fsync(arg->fd) < 0)
			perror("mixed_periodic_fsync");
	}
	return NULL;
}

int main(int argc, char **argv)
{
	const char *mode;
	int cycles, fd, i;
	char path[512];

	if (argc != 4) {
		fprintf(stderr, "usage: %s DIR punch|truncate|checkpoint|pf|cf|pt|pc|tc|ptc|pcf|tcf|ptf|ptcf|pff CYCLES\n",
			argv[0]);
		return 2;
	}
	mode = argv[2];
	cycles = atoi(argv[3]);
	if (cycles < 1 || cycles > 100000 ||
	    (strcmp(mode, "punch") && strcmp(mode, "truncate") &&
	     strcmp(mode, "checkpoint") && strcmp(mode, "pt") &&
	     strcmp(mode, "pf") && strcmp(mode, "cf") &&
	     strcmp(mode, "pc") && strcmp(mode, "tc") &&
	     strcmp(mode, "ptc") && strcmp(mode, "pcf") &&
	     strcmp(mode, "tcf") && strcmp(mode, "ptf") &&
	     strcmp(mode, "ptcf") && strcmp(mode, "pff")))
		return 2;
	if (mkdir(argv[1], 0700) < 0 && errno != EEXIST) {
		perror("mkdir");
		return 1;
	}
	snprintf(path, sizeof(path), "%s/target", argv[1]);
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
	if (fd < 0 || seed(fd) < 0) {
		perror("seed");
		return 1;
	}
	printf("mode=%s blocks=%d cycles=%d\n", mode, BLOCKS, cycles);
	fflush(stdout);
	if (strcmp(mode, "punch") && strcmp(mode, "truncate") &&
	    strcmp(mode, "checkpoint")) {
		pthread_t threads[4];
		struct mixed_args args[4];
		const char *ops = !strcmp(mode, "pt") ? "pt" :
			!strcmp(mode, "pf") ? "pf" :
			!strcmp(mode, "cf") ? "cf" :
			!strcmp(mode, "pc") ? "pc" :
			!strcmp(mode, "tc") ? "tc" :
			!strcmp(mode, "pcf") ? "pcf" :
			!strcmp(mode, "tcf") ? "tcf" :
			!strcmp(mode, "ptf") ? "ptf" :
			!strcmp(mode, "ptc") ? "ptc" :
			!strcmp(mode, "pff") ? "pff" : "ptcf";
		int nthreads = (int)strlen(ops);
		if (pthread_barrier_init(&mixed_barrier, NULL, nthreads))
			return 1;
		for (i = 0; i < nthreads; i++) {
			args[i].fd = fd;
			args[i].id = i;
			args[i].cycles = cycles;
			args[i].ops = ops;
			if (pthread_create(&threads[i], NULL, mixed_worker, &args[i]))
				return 1;
		}
		for (i = 0; i < nthreads; i++)
			pthread_join(threads[i], NULL);
		pthread_barrier_destroy(&mixed_barrier);
		if (fsync(fd) < 0)
			perror("final_fsync");
		close(fd);
		printf("finished mode=%s cycles=%d\n", mode, cycles);
		return 0;
	}
	for (i = 0; i < cycles; i++) {
		if (!strcmp(mode, "punch"))
			do_punch(fd, i);
		else if (!strcmp(mode, "truncate"))
			do_truncate(fd, i);
		else
			do_checkpoint(fd, i);
		if (!(i & 7) && fsync(fd) < 0)
			perror("periodic_fsync");
	}
	if (fsync(fd) < 0)
		perror("final_fsync");
	close(fd);
	printf("finished mode=%s cycles=%d\n", mode, cycles);
	return 0;
}
