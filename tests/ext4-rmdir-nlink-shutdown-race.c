// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define CHILDREN 1
#define WORKERS 1

#define PAUSE_PARAM "/sys/module/ext4/parameters/ext4_checker_rmdir_pause"
#define PAUSED_PARAM "/sys/module/ext4/parameters/ext4_checker_rmdir_paused"

struct state {
	const char *paths[CHILDREN];
	atomic_int next;
	atomic_int done;
	int rc[CHILDREN];
	int errors[CHILDREN];
};

static int child_fds[CHILDREN];

static int write_param(const char *path, char value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	ssize_t written;

	if (fd < 0)
		return -1;
	written = write(fd, &value, 1);
	close(fd);
	return written == 1 ? 0 : -1;
}

static int read_paused(void)
{
	char value = '0';
	int fd = open(PAUSED_PARAM, O_RDONLY | O_CLOEXEC);

	if (fd < 0)
		return -1;
	if (read(fd, &value, 1) != 1) {
		close(fd);
		return -1;
	}
	close(fd);
	return value == '1';
}

static void *rmdir_worker(void *arg)
{
	struct state *s = arg;
	cpu_set_t cpus;
	int index;

	CPU_ZERO(&cpus);
	CPU_SET(1, &cpus);
	(void)pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus);
	while ((index = atomic_fetch_add_explicit(&s->next, 1,
							 memory_order_relaxed)) < CHILDREN) {
		errno = 0;
		s->rc[index] = rmdir(s->paths[index]);
		s->errors[index] = errno;
		atomic_fetch_add_explicit(&s->done, 1, memory_order_release);
	}
	return NULL;
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/ext4-rmdir-nlink";
	char parent[256], paths[CHILDREN][256];
	struct state s = { 0 };
	struct stat before, after;
	pthread_t workers[WORKERS];
	uint32_t shutdown_flags = EXT4_GOING_FLAGS_NOLOGFLUSH;
	unsigned int spins = 0;
	int mountfd, parentfd, child, triggered = -1;
	int shutdown_rc, shutdown_errno, completed_before_shutdown;

	if (mkdir(mountpoint, 0700) && errno != EEXIST) {
		perror("mkdir mountpoint");
		return 1;
	}
	if (snprintf(parent, sizeof(parent), "%s/parent", mountpoint) >=
	    (int)sizeof(parent) || (mkdir(parent, 0700) && errno != EEXIST)) {
		perror("mkdir parent");
		return 1;
	}
	mountfd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	parentfd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (mountfd < 0 || parentfd < 0 || fstat(parentfd, &before)) {
		perror("open mount or parent");
		return 1;
	}

	for (child = 0; child < CHILDREN; child++) {
		if (snprintf(paths[child], sizeof(paths[child]), "%s/c%02d",
			     parent, child) >= (int)sizeof(paths[child]) ||
		    mkdir(paths[child], 0700)) {
			perror("mkdir child");
			return 1;
		}
		child_fds[child] = open(paths[child],
					       O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		if (child_fds[child] < 0) {
			perror("open child");
			return 1;
		}
		s.paths[child] = paths[child];
	}
	if (fstat(parentfd, &before)) {
		perror("fstat parent before");
		return 1;
	}
	if (write_param(PAUSE_PARAM, '1')) {
		perror("arm rmdir checker pause");
		return 1;
	}
	atomic_init(&s.next, 0);
	atomic_init(&s.done, 0);
	for (child = 0; child < WORKERS; child++) {
		if (pthread_create(&workers[child], NULL, rmdir_worker, &s)) {
			perror("pthread_create");
			return 1;
		}
	}

	for (spins = 0; spins < 10000; spins++) {
		int paused = read_paused();

		if (paused == 1) {
			triggered = 0;
			break;
		}
		if (paused < 0 || atomic_load_explicit(&s.done,
						       memory_order_acquire))
			break;
		usleep(1000);
	}

	completed_before_shutdown = atomic_load_explicit(&s.done,
							 memory_order_acquire);
	errno = 0;
	shutdown_rc = ioctl(mountfd, EXT4_IOC_SHUTDOWN, &shutdown_flags);
	shutdown_errno = errno;
	if (write_param(PAUSE_PARAM, '0')) {
		perror("release rmdir checker pause");
		return 1;
	}
	for (child = 0; child < WORKERS; child++)
		pthread_join(workers[child], NULL);
	if (fstat(parentfd, &after)) {
		perror("fstat parent after");
		return 1;
	}
	printf("trigger_child=%d spins=%u done_at_shutdown=%d/%d\n",
	       triggered, spins, completed_before_shutdown, CHILDREN);
	printf("shutdown_rc=%d errno=%d (%s)\n", shutdown_rc,
	       shutdown_rc < 0 ? shutdown_errno : 0,
	       shutdown_rc < 0 ? strerror(shutdown_errno) : "ok");
	printf("parent_nlink_before=%lu after=%lu rmdir_done=%d\n",
	       (unsigned long)before.st_nlink, (unsigned long)after.st_nlink,
	       atomic_load_explicit(&s.done, memory_order_relaxed));
	for (child = 0; child < CHILDREN; child++) {
		if (s.rc[child] < 0)
			printf("rmdir_child=%d errno=%d (%s)\n", child,
			       s.errors[child], strerror(s.errors[child]));
	}
	for (child = 0; child < CHILDREN; child++)
		close(child_fds[child]);
	close(parentfd);
	close(mountfd);
	return 0;
}
