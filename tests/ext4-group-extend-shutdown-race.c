// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ext4.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/statfs.h>
#include <unistd.h>

struct race_state {
	int resize_fd;
	uint32_t target_blocks;
	atomic_int started;
	atomic_int done;
	int resize_rc;
	int resize_errno;
};

static void pin_cpu(int cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	(void)pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

static void *extend_filesystem(void *arg)
{
	struct race_state *state = arg;

	pin_cpu(1);
	atomic_store_explicit(&state->started, 1, memory_order_release);
	errno = 0;
	state->resize_rc = ioctl(state->resize_fd, EXT4_IOC_GROUP_EXTEND,
				 &state->target_blocks);
	state->resize_errno = errno;
	atomic_store_explicit(&state->done, 1, memory_order_release);
	return NULL;
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/ext4-group-extend";
	uint32_t target_blocks = argc > 2 ? strtoul(argv[2], NULL, 0) : 65536;
	struct race_state state = { .target_blocks = target_blocks };
	struct statfs before, current, after;
	pthread_t worker;
	unsigned int shutdown_flags = EXT4_GOING_FLAGS_NOLOGFLUSH;
	int monitor_fd, shutdown_fd, shutdown_rc = -1, shutdown_errno = 0;
	unsigned int spins = 0;
	bool capacity_observed = false;

	state.resize_fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	monitor_fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	shutdown_fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (state.resize_fd < 0 || monitor_fd < 0 || shutdown_fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	if (fstatfs(monitor_fd, &before)) {
		perror("fstatfs before");
		return 1;
	}

	atomic_init(&state.started, 0);
	atomic_init(&state.done, 0);
	if (pthread_create(&worker, NULL, extend_filesystem, &state)) {
		perror("pthread_create");
		return 1;
	}
	pin_cpu(0);
	while (!atomic_load_explicit(&state.started, memory_order_acquire))
		sched_yield();
	while (!atomic_load_explicit(&state.done, memory_order_acquire)) {
		if (!fstatfs(monitor_fd, &current) && current.f_blocks > before.f_blocks) {
			capacity_observed = true;
			errno = 0;
			shutdown_rc = ioctl(shutdown_fd, EXT4_IOC_SHUTDOWN,
					    &shutdown_flags);
			shutdown_errno = errno;
			break;
		}
		if (++spins == 1000000)
			break;
	}
	pthread_join(worker, NULL);
	if (fstatfs(monitor_fd, &after))
		memset(&after, 0, sizeof(after));

	printf("before_blocks=%" PRIu64 " target_blocks=%" PRIu32 "\n",
	       (uint64_t)before.f_blocks, target_blocks);
	printf("capacity_observed=%d monitor_spins=%u\n",
	       capacity_observed, spins);
	printf("shutdown_rc=%d errno=%d (%s)\n", shutdown_rc,
	       shutdown_rc < 0 ? shutdown_errno : 0,
	       shutdown_rc < 0 ? strerror(shutdown_errno) : "ok");
	printf("group_extend_rc=%d errno=%d (%s)\n", state.resize_rc,
	       state.resize_rc < 0 ? state.resize_errno : 0,
	       state.resize_rc < 0 ? strerror(state.resize_errno) : "ok");
	printf("after_blocks=%" PRIu64 " after_bfree=%" PRIu64 "\n",
	       (uint64_t)after.f_blocks, (uint64_t)after.f_bfree);
	close(shutdown_fd);
	close(monitor_fd);
	close(state.resize_fd);
	return 0;
}
