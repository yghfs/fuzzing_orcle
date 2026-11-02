// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <pthread.h>
#include <sched.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

struct state {
	int dirfd;
	int mountfd;
	atomic_int start;
	atomic_int stop;
	atomic_int mkdir_errors;
	atomic_int created;
};

static void *mkdir_worker(void *arg)
{
	struct state *s = arg;
	unsigned int i = 0;
	char name[64];

	while (!atomic_load(&s->start))
		sched_yield();
	while (!atomic_load(&s->stop)) {
		snprintf(name, sizeof(name), "child-%u", i++);
		if (!mkdirat(s->dirfd, name, 0700)) {
			atomic_fetch_add(&s->created, 1);
			continue;
		}
		atomic_fetch_add(&s->mkdir_errors, 1);
		if (errno == EIO || errno == EROFS || errno == ENOSPC)
			continue;
		if (errno == EEXIST)
			continue;
	}
	return NULL;
}

int main(int argc, char **argv)
{
	const char *mnt = argc > 1 ? argv[1] : "/mnt/ext4-mkdir-shutdown";
	struct state s;
	struct stat before, after;
	pthread_t threads[8];
	unsigned int mode = argc > 2 ? strtoul(argv[2], NULL, 0) :
		EXT4_GOING_FLAGS_DEFAULT;
	int watch_parent = argc > 3 && !strcmp(argv[3], "parent_nlink");
	int rc;

	if (mkdir(mnt, 0700) && errno != EEXIST)
		return 1;
	s.dirfd = open(mnt, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	s.mountfd = open(mnt, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (s.dirfd < 0 || s.mountfd < 0)
		return 1;
	if (fstat(s.dirfd, &before))
		return 1;
	atomic_init(&s.start, 0);
	atomic_init(&s.stop, 0);
	atomic_init(&s.mkdir_errors, 0);
	atomic_init(&s.created, 0);
	for (size_t i = 0; i < 8; i++)
		pthread_create(&threads[i], NULL, mkdir_worker, &s);
	atomic_store(&s.start, 1);
	if (watch_parent) {
		struct stat current;
		unsigned int spins;

		for (spins = 0; spins < 1000000; spins++) {
			if (!fstat(s.dirfd, &current) &&
			    current.st_nlink > before.st_nlink)
				break;
			sched_yield();
		}
		printf("parent_nlink_triggered=%d spins=%u\n",
		       spins < 1000000, spins);
	} else {
		usleep(20000);
	}
	rc = ioctl(s.mountfd, EXT4_IOC_SHUTDOWN, &mode);
	printf("watch_parent=%d shutdown_flags=%u shutdown_rc=%d errno=%d (%s)\n",
	       watch_parent, mode,
	       rc, rc ? errno : 0,
	       rc ? strerror(errno) : "ok");
	atomic_store(&s.stop, 1);
	for (size_t i = 0; i < 8; i++)
		pthread_join(threads[i], NULL);
	if (fstat(s.dirfd, &after))
		return 1;
	printf("parent_nlink_before=%lu after=%lu created=%d errors=%d\n",
	       (unsigned long)before.st_nlink, (unsigned long)after.st_nlink,
	       atomic_load(&s.created), atomic_load(&s.mkdir_errors));
	close(s.mountfd);
	close(s.dirfd);
	return 0;
}
