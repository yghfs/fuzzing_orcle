// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef EA_COUNT
#define EA_COUNT 64
#endif
#define VALUE_LEN 5000

static void fail(const char *what)
{
	perror(what);
	exit(1);
}

static void set_value(int fd, unsigned int index, unsigned int generation)
{
	char name[32];
	unsigned char value[VALUE_LEN];

	snprintf(name, sizeof(name), "user.a%03u", index);
	for (size_t j = 0; j < sizeof(value); j++)
		value[j] = (unsigned char)(j * 31 + index * 3 + generation * 11);
	if (fsetxattr(fd, name, value, sizeof(value), 0))
		fail("fsetxattr");
}

static pid_t pin_memory(size_t mib)
{
	int ready[2];
	pid_t pid;

	if (!mib)
		return -1;
	if (pipe(ready))
		fail("pipe");
	pid = fork();
	if (pid < 0)
		fail("fork");
	if (!pid) {
		long page = sysconf(_SC_PAGESIZE);
		size_t bytes = mib * 1024 * 1024;
		void *memory;
		char ok = 'R';
		struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
		close(ready[0]);
		(void)setrlimit(RLIMIT_MEMLOCK, &limit);
		memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (memory == MAP_FAILED)
			_exit(20);
		for (size_t off = 0; off < bytes; off += page)
			*((volatile char *)memory + off) = (char)(off >> 12);
		if (mlock(memory, bytes))
			_exit(21);
		if (write(ready[1], &ok, 1) != 1)
			_exit(22);
		close(ready[1]);
		for (;;)
			pause();
	}
	close(ready[1]);
	{
		char ok;
		if (read(ready[0], &ok, 1) != 1) {
			close(ready[0]);
			waitpid(pid, NULL, 0);
			errno = ENOMEM;
			fail("pin memory pressure");
		}
	}
	close(ready[0]);
	return pid;
}

static void drop_caches(void)
{
	int fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);
	const char value[] = "2\n";
	if (fd < 0)
		fail("open drop_caches");
	if (write(fd, value, sizeof(value) - 1) != sizeof(value) - 1)
		fail("write drop_caches");
	close(fd);
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-ea-clone/work";
	unsigned int rounds = argc > 2 ? strtoul(argv[2], NULL, 10) : 100;
	size_t pressure_mib = argc > 3 ? strtoul(argv[3], NULL, 10) : 300;
	char path_a[256], path_b[256];
	int a, b, dirfd;
	pid_t pressure;

	if (!rounds)
		return 2;
	if (mkdir(dir, 0700) && errno != EEXIST)
		fail("mkdir");
	snprintf(path_a, sizeof(path_a), "%s/a", dir);
	snprintf(path_b, sizeof(path_b), "%s/b", dir);
	a = open(path_a, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	b = open(path_b, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (a < 0 || b < 0)
		fail("open files");
	for (unsigned int i = 0; i < EA_COUNT; i++) {
		set_value(a, i, 1);
		set_value(b, i, 1);
	}
	if (fsync(a) || fsync(b))
		fail("fsync seed");
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0 || syncfs(dirfd))
		fail("syncfs seed");
	drop_caches();
	pressure = pin_memory(pressure_mib);
	printf("ea_clone_pressure_ready rounds=%u ea_count=%u pressure_mib=%zu\n",
	       rounds, EA_COUNT, pressure_mib);
	fflush(stdout);
	for (unsigned int r = 0; r < rounds; r++) {
		for (unsigned int i = 0; i < EA_COUNT; i++) {
			set_value(a, i, r + 2);
			set_value(b, i, r + 2);
		}
		if (fsync(a) || fsync(b))
			fail("fsync shared block");
		drop_caches();
		set_value(a, r % EA_COUNT, r + 3);
		if (!(r % 4)) {
			printf("clone_update_round=%u\n", r);
			fflush(stdout);
		}
	}
	if (pressure > 0) {
		kill(pressure, SIGTERM);
		waitpid(pressure, NULL, 0);
	}
	if (fsync(a) || fsync(b) || syncfs(dirfd))
		fail("syncfs result");
	close(a);
	close(b);
	close(dirfd);
	printf("ea_clone_rollback_workload_complete rounds=%u\n", rounds);
	return 0;
}
