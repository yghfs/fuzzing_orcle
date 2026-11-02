// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <stdbool.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_WORKERS 32
#define DEFAULT_WORKERS 8
#define DEFAULT_ITERATIONS 1000
#define FILE_SIZE (64 * 1024)

static void die(const char *what)
{
	fprintf(stderr, "%s: %s\n", what, strerror(errno));
	_exit(1);
}

static void pause_briefly(void)
{
	struct timespec delay = { .tv_sec = 0, .tv_nsec = 100000 };

	while (nanosleep(&delay, &delay) && errno == EINTR)
		;
}

static void worker(int dirfd, unsigned int id, unsigned int iterations)
{
	char name[64];
	unsigned int completed = 0;

	for (unsigned int i = 0; i < iterations; i++) {
		int fd;

		snprintf(name, sizeof(name), "w%u-%u", id, i);
		fd = openat(dirfd, name,
			    O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0)
			die("openat");
		if (fallocate(fd, 0, 0, FILE_SIZE) < 0)
			die("fallocate");
		if (fsync(fd) < 0)
			die("fsync allocated file");
		if (ftruncate(fd, 0) < 0)
			die("truncate allocated file");
		if (fallocate(fd, 0, 0, FILE_SIZE) < 0)
			die("reallocate file");
		if (unlinkat(dirfd, name, 0) < 0)
			die("unlinkat open file");
		/* Keep the unlinked inode orphaned while other workers churn slots. */
		pause_briefly();
		if (close(fd) < 0)
			die("close orphaned file");
		completed++;
	}
	printf("orphan_worker=%u completed=%u\n", id, completed);
	_exit(0);
}

static void worker_hold(int dirfd, unsigned int id, unsigned int iterations,
			int ready_fd, int release_fd)
{
	int *fds = calloc(iterations, sizeof(*fds));
	unsigned int held = 0;
	char name[64], release;
	struct rlimit fd_limit = {
		.rlim_cur = (rlim_t)iterations + 64,
		.rlim_max = (rlim_t)iterations + 64,
	};

	if (!fds)
		die("allocate fd table");
	if (setrlimit(RLIMIT_NOFILE, &fd_limit) < 0)
		die("raise worker fd limit");
	for (unsigned int i = 0; i < iterations; i++) {
		int fd;

		snprintf(name, sizeof(name), "held-%u-%u", id, i);
		fd = openat(dirfd, name,
			    O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			if (errno == ENOSPC || errno == EDQUOT || errno == EMFILE)
				break;
			die("open orphan candidate");
		}
		if (unlinkat(dirfd, name, 0) < 0)
			die("unlink open orphan candidate");
		fds[held++] = fd;
	}
	if (write(ready_fd, &held, sizeof(held)) != sizeof(held))
		die("signal orphan hold ready");
	if (read(release_fd, &release, sizeof(release)) != sizeof(release))
		die("wait for orphan hold release");
	for (unsigned int i = 0; i < held; i++)
		if (close(fds[i]) < 0)
			die("close held orphan");
	free(fds);
	_exit(0);
}

int main(int argc, char **argv)
{
	int workers = argc > 2 ? atoi(argv[2]) : DEFAULT_WORKERS;
	int iterations = argc > 3 ? atoi(argv[3]) : DEFAULT_ITERATIONS;
	int dirfd;
	pid_t pids[MAX_WORKERS];
	unsigned long operations;
	unsigned int hold_seconds = argc > 5 ? atoi(argv[5]) : 30;
	bool hold_orphans = argc > 4 && !strcmp(argv[4], "hold");

	if (argc < 2 || workers < 1 || workers > MAX_WORKERS || iterations < 1 ||
	    hold_seconds > 300) {
		fprintf(stderr, "usage: %s DIR [WORKERS [ITERATIONS [hold [SECONDS]]]]\n",
			argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700) < 0)
		die("mkdir workload directory");
	dirfd = open(argv[1], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		die("open workload directory");
	if (hold_orphans) {
		int ready_pipe[2], release_pipe[2];
		unsigned int ready[MAX_WORKERS], held_total = 0;
		char release[MAX_WORKERS];
		int marker_fd;

		if (pipe(ready_pipe) || pipe(release_pipe))
			die("create orphan hold pipes");
		for (int i = 0; i < workers; i++) {
			pids[i] = fork();
			if (pids[i] < 0)
				die("fork orphan hold worker");
			if (!pids[i]) {
				close(ready_pipe[0]);
				close(release_pipe[1]);
				worker_hold(dirfd, i, iterations, ready_pipe[1],
					    release_pipe[0]);
			}
		}
		close(ready_pipe[1]);
		close(release_pipe[0]);
		for (int i = 0; i < workers; i++) {
			ssize_t bytes;

			do {
				bytes = read(ready_pipe[0], &ready[i], sizeof(ready[i]));
			} while (bytes < 0 && errno == EINTR);
			if (bytes != sizeof(ready[i]))
				die("wait for orphan hold workers");
		}
		close(ready_pipe[0]);
		for (int i = 0; i < workers; i++)
			held_total += ready[i];
		marker_fd = open("/tmp/ext4-orphan-hold-ready",
				 O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0600);
		if (marker_fd < 0)
			die("create orphan hold marker");
		if (dprintf(marker_fd,
			    "workers=%d iterations=%d held=%u\n",
			    workers, iterations, held_total) < 0 ||
		    close(marker_fd) < 0)
			die("write orphan hold marker");
		operations = 2UL * workers * iterations;
		printf("orphan_hold_ready=1 workers=%d iterations=%d held=%u "
		       "syscalls_estimate=%lu\n", workers, iterations,
		       held_total, operations);
		fflush(stdout);
		while (hold_seconds)
			hold_seconds = sleep(hold_seconds);
		memset(release, 'G', workers);
		if (write(release_pipe[1], release, workers) != workers)
			die("release orphan hold workers");
		close(release_pipe[1]);
		for (int i = 0; i < workers; i++) {
			int status;

			if (waitpid(pids[i], &status, 0) != pids[i])
				die("wait orphan hold worker");
			if (!WIFEXITED(status) || WEXITSTATUS(status))
				return 1;
		}
		unlink("/tmp/ext4-orphan-hold-ready");
		printf("orphan_hold_released=1\n");
		return 0;
	}
	for (int i = 0; i < workers; i++) {
		pids[i] = fork();
		if (pids[i] < 0)
			die("fork");
		if (!pids[i])
			worker(dirfd, i, iterations);
	}
	for (int i = 0; i < workers; i++) {
		int status;

		if (waitpid(pids[i], &status, 0) != pids[i])
			die("waitpid");
		if (!WIFEXITED(status) || WEXITSTATUS(status)) {
			fprintf(stderr, "worker %d failed, status=%d\n", i, status);
			return 1;
		}
	}
	if (syncfs(dirfd) < 0)
		die("syncfs");
	operations = (unsigned long)workers * iterations * 8;
	printf("orphan_file_count_stress=complete workers=%d iterations=%d "
	       "syscalls_estimate=%lu\n", workers, iterations, operations);
	return 0;
}
