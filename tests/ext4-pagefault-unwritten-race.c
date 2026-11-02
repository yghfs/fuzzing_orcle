#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define FILE_SIZE (2UL * 1024 * 1024)
#define PAGE_SIZE 4096UL

static void handle_sigbus(int signal)
{
	ssize_t written;

	(void)signal;
	written = write(STDERR_FILENO, "caught SIGBUS during mapped access\n", 35);
	(void)written;
	_exit(128 + SIGBUS);
}

struct run_state {
	int fd;
	int direct_fd;
	unsigned int iterations;
	volatile unsigned char *mapping;
	void *direct_buffer;
	unsigned long mmap_writes;
	unsigned long direct_writes;
	unsigned long range_ops;
	unsigned long errors;
	unsigned long range_errors;
	unsigned long direct_errors;
};

static void *mapped_writer(void *arg)
{
	struct run_state *state = arg;
	unsigned int i;

	for (i = 0; i < state->iterations; i++) {
		size_t offset = ((size_t)i * 7919) % FILE_SIZE;

		state->mapping[offset] = (unsigned char)i;
		state->mmap_writes++;
		if (!(i % 100))
			fprintf(stderr, "mapped_writes=%u offset=%zu\n", i + 1, offset);
	}
	return NULL;
}

static void *range_mutator(void *arg)
{
	struct run_state *state = arg;
	unsigned int i;

	for (i = 0; i < state->iterations; i++) {
		off_t offset = ((off_t)i * 65536) % (FILE_SIZE - 65536);
		int mode;

		switch (i % 3) {
		case 0:
			mode = FALLOC_FL_ZERO_RANGE;
			break;
		case 1:
			mode = FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE;
			break;
		default:
			mode = FALLOC_FL_KEEP_SIZE;
			break;
		}
		if (fallocate(state->fd, mode, offset, PAGE_SIZE) &&
		    errno != EOPNOTSUPP && errno != EINVAL) {
			fprintf(stderr, "range_error mode=%x offset=%lld errno=%d\n",
				mode, (long long)offset, errno);
			state->range_errors++;
			__atomic_fetch_add(&state->errors, 1, __ATOMIC_RELAXED);
		}
		state->range_ops++;
	}
	return NULL;
}

static void *direct_writer(void *arg)
{
	struct run_state *state = arg;
	unsigned int i;

	for (i = 0; i < state->iterations; i++) {
		off_t offset = ((off_t)i * PAGE_SIZE) % FILE_SIZE;
		ssize_t written;

		memset(state->direct_buffer, (int)i, PAGE_SIZE);
		written = pwrite(state->direct_fd, state->direct_buffer,
				 PAGE_SIZE, offset);
		if (written == PAGE_SIZE)
			state->direct_writes++;
		else if ((written < 0 && errno != EINVAL && errno != EOPNOTSUPP) ||
			(written >= 0 && written != PAGE_SIZE)) {
			fprintf(stderr, "direct_error offset=%lld written=%lld errno=%d\n",
				(long long)offset, (long long)written, errno);
			state->direct_errors++;
			__atomic_fetch_add(&state->errors, 1, __ATOMIC_RELAXED);
		}
	}
	return NULL;
}

int main(int argc, char **argv)
{
	struct run_state state = { .direct_fd = -1, .iterations = 2000 };
	pthread_t mapped_thread, range_thread, direct_thread;
	const char *mode = argc > 2 ? argv[2] : "all";
	bool run_range = strcmp(mode, "direct-only") && strcmp(mode, "mmap-only");
	bool run_direct = strcmp(mode, "range-only") && strcmp(mode, "mmap-only");
	int create_fd = -1;
	int ret = 1;
	char verify_buffer[64 * 1024];
	size_t verified = 0;
	struct stat file_stat;
	struct sigaction action = { .sa_handler = handle_sigbus };

	if (argc > 1)
		state.iterations = (unsigned int)strtoul(argv[1], NULL, 0);
	if (!state.iterations)
		return 2;
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGBUS, &action, NULL))
		return 2;
	create_fd = open("/tmp/ext4-pagefault-unwritten-data", O_CREAT | O_TRUNC | O_RDWR, 0600);
	fprintf(stderr, "created_fd=%d errno=%d\n", create_fd, errno);
	if (create_fd < 0 || ftruncate(create_fd, FILE_SIZE) ||
	    fallocate(create_fd, 0, 0, FILE_SIZE))
		goto out;
	fprintf(stderr, "file_ready size=%lu mode=%s\n", FILE_SIZE, mode);
	state.fd = create_fd;
	if (run_direct) {
		state.direct_fd = open("/tmp/ext4-pagefault-unwritten-data",
				       O_RDWR | O_DIRECT);
		if (state.direct_fd < 0 ||
		    posix_memalign(&state.direct_buffer, PAGE_SIZE, PAGE_SIZE))
			goto out;
	}
	state.mapping = mmap(NULL, FILE_SIZE, PROT_READ | PROT_WRITE,
			     MAP_SHARED, state.fd, 0);
	if (state.mapping == MAP_FAILED)
		goto out;
	fprintf(stderr, "mapping_ready address=%p\n", (void *)state.mapping);
	if (pthread_create(&mapped_thread, NULL, mapped_writer, &state))
		goto out;
	if (run_range && pthread_create(&range_thread, NULL, range_mutator, &state)) {
		pthread_join(mapped_thread, NULL);
		goto out;
	}
	if (run_direct && pthread_create(&direct_thread, NULL, direct_writer, &state)) {
		pthread_join(mapped_thread, NULL);
		if (run_range)
			pthread_join(range_thread, NULL);
		goto out;
	}
	pthread_join(mapped_thread, NULL);
	if (run_range)
		pthread_join(range_thread, NULL);
	if (run_direct)
		pthread_join(direct_thread, NULL);
	if (fsync(state.fd))
		state.errors++;
	if (run_direct && fsync(state.direct_fd))
		state.errors++;
	if (fstat(state.fd, &file_stat) || file_stat.st_size != FILE_SIZE)
		state.errors++;
	while (verified < FILE_SIZE) {
		size_t count = sizeof(verify_buffer);
		ssize_t bytes;

		if (count > FILE_SIZE - verified)
			count = FILE_SIZE - verified;
		bytes = pread(state.fd, verify_buffer, count, verified);
		if (bytes <= 0) {
			state.errors++;
			break;
		}
		verified += bytes;
	}
	printf("iterations=%u mmap_writes=%lu direct_writes=%lu range_ops=%lu readback=%zu range_errors=%lu direct_errors=%lu errors=%lu\n",
	       state.iterations, state.mmap_writes, state.direct_writes,
	       state.range_ops, verified, state.range_errors, state.direct_errors,
	       state.errors);
	ret = state.errors ? 1 : 0;
out:
	if (state.mapping && state.mapping != MAP_FAILED)
		munmap((void *)state.mapping, FILE_SIZE);
	if (state.direct_buffer)
		free(state.direct_buffer);
	if (state.direct_fd > 0)
		close(state.direct_fd);
	if (create_fd >= 0)
		close(create_fd);
	if (ret)
		perror("ext4 pagefault/unwritten workload");
	return ret;
}
