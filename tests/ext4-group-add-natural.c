// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ext4.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/statfs.h>
#include <unistd.h>

static int print_statfs(int fd, const char *label)
{
	struct statfs st;

	if (fstatfs(fd, &st) < 0)
		return -1;
	printf("%s_blocks=%" PRIu64 " %s_bfree=%" PRIu64
	       " %s_files=%" PRIu64 " %s_ffree=%" PRIu64 "\n",
	       label, (uint64_t)st.f_blocks, label, (uint64_t)st.f_bfree,
	       label, (uint64_t)st.f_files, label, (uint64_t)st.f_ffree);
	return 0;
}

struct add_state {
	int fd;
	struct ext4_new_group_input input;
	atomic_int started;
	atomic_int done;
	int rc;
	int error;
};

static void *add_group(void *arg)
{
	struct add_state *state = arg;
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(1, &set);
	(void)pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
	atomic_store_explicit(&state->started, 1, memory_order_release);
	errno = 0;
	state->rc = ioctl(state->fd, EXT4_IOC_GROUP_ADD, &state->input);
	state->error = errno;
	atomic_store_explicit(&state->done, 1, memory_order_release);
	return NULL;
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/group-add";
	uint32_t group = argc > 2 ? strtoul(argv[2], NULL, 0) : 16;
	uint64_t start = argc > 3 ? strtoull(argv[3], NULL, 0) : 524288;
	uint32_t blocks = argc > 4 ? strtoul(argv[4], NULL, 0) : 32768;
	uint32_t table_blocks = argc > 5 ? strtoul(argv[5], NULL, 0) : 512;
	struct ext4_new_group_input in = {
		.group = group,
		.block_bitmap = start,
		.inode_bitmap = start + 1,
		.inode_table = start + 2,
		.blocks_count = blocks,
		.reserved_blocks = 0,
	};
	struct add_state state = { .input = in };
	struct statfs before, current;
	pthread_t worker;
	int fd, monitor_fd = -1, shutdown_fd = -1, rc, saved_errno;
	int race_mode = argc > 6 ? atoi(argv[6]) : 0;
	unsigned int shutdown_flags = argc > 7 ? strtoul(argv[7], NULL, 0) :
		EXT4_GOING_FLAGS_NOLOGFLUSH;
	int shutdown_rc = -1, shutdown_error = 0;
	unsigned int spins = 0;
	bool publication_observed = false;

	if (table_blocks + 2 >= blocks) {
		fprintf(stderr, "invalid group geometry\n");
		return 2;
	}
	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	if (print_statfs(fd, "before")) {
		perror("statfs before");
		return 1;
	}
	if (!race_mode) {
		errno = 0;
		rc = ioctl(fd, EXT4_IOC_GROUP_ADD, &in);
		saved_errno = errno;
	} else {
		monitor_fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		shutdown_fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		if (monitor_fd < 0 || shutdown_fd < 0 ||
		    fstatfs(monitor_fd, &before)) {
			perror("open race descriptors or statfs");
			return 1;
		}
		state.fd = fd;
		atomic_init(&state.started, 0);
		atomic_init(&state.done, 0);
		if (pthread_create(&worker, NULL, add_group, &state)) {
			perror("pthread_create");
			return 1;
		}
		while (!atomic_load_explicit(&state.started, memory_order_acquire))
			sched_yield();
		while (!atomic_load_explicit(&state.done, memory_order_acquire)) {
			if (!fstatfs(monitor_fd, &current) &&
			    current.f_files > before.f_files) {
				publication_observed = true;
				errno = 0;
				shutdown_rc = ioctl(shutdown_fd, EXT4_IOC_SHUTDOWN,
						    &shutdown_flags);
				shutdown_error = errno;
				break;
			}
			if (++spins == 1000000)
				break;
		}
		pthread_join(worker, NULL);
		rc = state.rc;
		saved_errno = state.error;
		printf("publication_observed=%d spins=%u shutdown_rc=%d errno=%d (%s)\n",
		       publication_observed, spins, shutdown_rc,
		       shutdown_rc < 0 ? shutdown_error : 0,
	       shutdown_rc < 0 ? strerror(shutdown_error) : "ok");
		close(shutdown_fd);
		close(monitor_fd);
	}
	printf("group_add group=%" PRIu32 " start=%" PRIu64
	       " blocks=%" PRIu32 " table_blocks=%" PRIu32
	       " expected_free_clusters=%" PRIu32 " rc=%d errno=%d (%s)\n",
	       group, start, blocks, table_blocks, blocks - table_blocks - 2,
	       rc, saved_errno, strerror(saved_errno));
	if (print_statfs(fd, "after")) {
		perror("statfs after");
		return 1;
	}
	if (rc == 0 && syncfs(fd) < 0) {
		perror("syncfs");
		return 1;
	}
	close(fd);
	return rc < 0 ? 2 : 0;
}
