// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void set_oom_score(const char *value)
{
	int fd = open("/proc/self/oom_score_adj", O_WRONLY | O_CLOEXEC);

	if (fd >= 0) {
		ssize_t written = write(fd, value, strlen(value));
		(void)written;
		close(fd);
	}
}

static pid_t start_pressure(size_t bytes)
{
	int ready[2];
	pid_t pid;

	if (pipe(ready) < 0)
		return -1;
	set_oom_score("-1000\n");
	pid = fork();
	if (pid < 0) {
		close(ready[0]);
		close(ready[1]);
		return -1;
	}
	if (!pid) {
		struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
		long page = sysconf(_SC_PAGESIZE);
		unsigned char *memory;
		char result = '0';

		close(ready[0]);
		set_oom_score("1000\n");
		if (setrlimit(RLIMIT_MEMLOCK, &limit) == 0) {
			memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
				      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			if (memory != MAP_FAILED) {
				for (size_t off = 0; off < bytes; off += page)
					memory[off] = (unsigned char)off;
				if (mlock(memory, bytes) == 0)
					result = '1';
			}
		}
		ssize_t written = write(ready[1], &result, 1);
		(void)written;
		close(ready[1]);
		if (result != '1')
			_exit(2);
		for (;;)
			pause();
	}
	close(ready[1]);
	{
		char result = '0';
		ssize_t n = read(ready[0], &result, 1);

		close(ready[0]);
		if (n != 1 || result != '1') {
			(void)kill(pid, SIGTERM);
			(void)waitpid(pid, NULL, 0);
			errno = ENOMEM;
			return -1;
		}
	}
	return pid;
}

static int prepare_orphan(int dirfd, unsigned int round, unsigned int index)
{
	char name[64];
	char data[4096];
	int fd;

	snprintf(name, sizeof(name), "victim-%u-%u", round, index);
	memset(data, 'V', sizeof(data));
	fd = openat(dirfd, name, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	if (write(fd, data, sizeof(data)) != (ssize_t)sizeof(data) ||
	    fsync(fd) < 0 || unlinkat(dirfd, name, 0) < 0) {
		int saved = errno;
		close(fd);
		errno = saved;
		return -1;
	}
	return fd;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-evict-pressure";
	unsigned int rounds = argc > 2 ? strtoul(argv[2], NULL, 0) : 8;
	unsigned int orphan_count = argc > 3 ? strtoul(argv[3], NULL, 0) : 64;
	unsigned int pressure_mb = argc > 4 ? strtoul(argv[4], NULL, 0) : 320;
	int dirfd, *fds;
	unsigned int close_errors = 0, prepare_errors = 0, pressure_errors = 0;

	if (!rounds || !orphan_count || orphan_count > 1024 || !pressure_mb)
		return 2;
	if (mkdir(dir, 0700) < 0 && errno != EEXIST) {
		perror("mkdir workload");
		return 1;
	}
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0) {
		perror("open workload");
		return 1;
	}
	fds = calloc(orphan_count, sizeof(*fds));
	if (!fds)
		return 1;
	for (unsigned int round = 0; round < rounds; round++) {
		pid_t pressures[2] = { -1, -1 };

		for (unsigned int i = 0; i < orphan_count; i++) {
			fds[i] = prepare_orphan(dirfd, round, i);
			if (fds[i] < 0) {
				prepare_errors++;
				break;
			}
		}
		if (prepare_errors)
			break;
		for (unsigned int worker = 0; worker < 2; worker++) {
			pressures[worker] = start_pressure(
				(size_t)pressure_mb * 1024 * 1024);
			if (pressures[worker] < 0)
				break;
		}
		if (pressures[0] < 0 || pressures[1] < 0) {
			perror("start memory pressure");
			pressure_errors++;
			for (unsigned int worker = 0; worker < 2; worker++)
				if (pressures[worker] > 0)
					(void)kill(pressures[worker], SIGTERM);
			for (unsigned int worker = 0; worker < 2; worker++)
				if (pressures[worker] > 0)
					(void)waitpid(pressures[worker], NULL, 0);
			for (unsigned int i = 0; i < orphan_count; i++)
				close(fds[i]);
			break;
		}
		printf("round=%u pressure_ready=2 orphans=%u pressure_mb=%u\n",
		       round, orphan_count, pressure_mb);
		fflush(stdout);
		for (unsigned int i = 0; i < orphan_count; i++) {
			if (close(fds[i]) < 0)
				close_errors++;
			fds[i] = -1;
		}
		for (unsigned int worker = 0; worker < 2; worker++) {
			(void)kill(pressures[worker], SIGTERM);
			(void)waitpid(pressures[worker], NULL, 0);
		}
		if (syncfs(dirfd) < 0)
			perror("syncfs");
	}
	printf("evict_pressure_complete rounds=%u close_errors=%u "
	       "prepare_errors=%u pressure_errors=%u\n", rounds, close_errors,
	       prepare_errors, pressure_errors);
	free(fds);
	close(dirfd);
	return prepare_errors || pressure_errors ? 1 : 0;
}
