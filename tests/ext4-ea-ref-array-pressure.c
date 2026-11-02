// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/resource.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <sys/wait.h>
#include <unistd.h>

#define VALUE_LEN 5000
#define EA_COUNT 32

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static pid_t pin_memory(size_t bytes)
{
	int ready[2];
	pid_t pid;
	if (pipe(ready) < 0)
		die("pipe");
	pid = fork();
	if (pid < 0)
		die("fork");
	if (!pid) {
		struct rlimit lim = { RLIM_INFINITY, RLIM_INFINITY };
		long page = sysconf(_SC_PAGESIZE);
		char *p;
		close(ready[0]);
		/* Keep the pressure worker alive long enough to cover the teardown.
		 * The parent remains the useful syscall driver; if reclaim reaches
		 * OOM, prefer reclaiming another guest task over dropping the driver
		 * before the allocation-failure window is exercised. */
		(void)setpriority(PRIO_PROCESS, 0, 19);
		{
			int oom = open("/proc/self/oom_score_adj", O_WRONLY);
			if (oom >= 0) {
				ssize_t wr = write(oom, "-500", 4);
				if (wr != 4)
					dprintf(STDERR_FILENO, "oom_score_adj write=%zd errno=%d\n",
						wr, errno);
				close(oom);
			}
		}
		setrlimit(RLIMIT_MEMLOCK, &lim);
		p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (p == MAP_FAILED)
			_exit(20);
		for (size_t off = 0; off < bytes; off += page)
			p[off] = (char)(off >> 12);
		if (mlock(p, bytes) < 0)
			_exit(21);
		if (write(ready[1], "R", 1) != 1)
			_exit(22);
		for (;;)
			pause();
	}
	close(ready[1]);
	{
		char c;
		if (read(ready[0], &c, 1) != 1) {
			int status;
			waitpid(pid, &status, 0);
			errno = ENOMEM;
			die("mlock pressure");
		}
	}
	close(ready[0]);
	return pid;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ea-array";
	size_t pressure_mib = argc > 2 ? strtoul(argv[2], NULL, 0) : 220;
	long min_free_kib = argc > 3 ? strtol(argv[3], NULL, 0) : 0;
	char path[256], name[64], value[VALUE_LEN];
	int fd;
	pid_t pressure;
	{
		int oom = open("/proc/self/oom_score_adj", O_WRONLY);
		if (oom >= 0) {
			ssize_t wr = write(oom, "-1000", 5);
			if (wr != 5)
				dprintf(STDERR_FILENO, "parent oom_score_adj write=%zd errno=%d\n",
					wr, errno);
			close(oom);
		}
	}

	if (mkdir(dir, 0700) < 0 && errno != EEXIST)
		die("mkdir");
	snprintf(path, sizeof(path), "%s/many-ea", dir);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("open");
	for (unsigned int i = 0; i < EA_COUNT; i++) {
		snprintf(name, sizeof(name), "user.array.%02u", i);
		memset(value, 'A' + (i % 26), sizeof(value));
		if (fsetxattr(fd, name, value, sizeof(value), 0) < 0) {
			fprintf(stderr, "fsetxattr index=%u name=%s errno=%d\n", i,
				name, errno);
			die("fsetxattr");
		}
	}
	if (fsync(fd) < 0)
		die("fsync seed");
	printf("seeded_ea_count=%u\n", EA_COUNT);
	fflush(stdout);
	if (min_free_kib > 0) {
		FILE *vm = fopen("/proc/sys/vm/min_free_kbytes", "we");
		if (!vm || fprintf(vm, "%ld\n", min_free_kib) < 0 || fclose(vm))
			die("set min_free_kbytes");
		printf("min_free_kbytes=%ld\n", min_free_kib);
		fflush(stdout);
	}
	pressure = pressure_mib ? pin_memory(pressure_mib * 1024 * 1024) : -1;
	if (unlink(path) < 0)
		die("unlink");
	if (close(fd) < 0)
		die("close");
	printf("unlinked_under_pressure=%u pressure_mib=%zu\n", EA_COUNT,
	       pressure_mib);
	fflush(stdout);
	if (pressure > 0) {
		kill(pressure, SIGTERM);
		waitpid(pressure, NULL, 0);
	}
	if (syncfs(open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC)) < 0)
		die("syncfs");
	printf("ea_array_pressure_complete\n");
	return 0;
}
