// SPDX-License-Identifier: GPL-2.0-only
/* Natural EA-inode reference-release pressure using concurrent allocator churn. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef EA_COUNT
#define EA_COUNT 128
#endif
#define VALUE_LEN 5000

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void set_oom_score_adj(int score)
{
	char value[32];
	int fd = open("/proc/self/oom_score_adj", O_WRONLY | O_CLOEXEC);
	int len;
	ssize_t written;

	if (fd < 0)
		return;
	len = snprintf(value, sizeof(value), "%d\n", score);
	if (len > 0) {
		written = write(fd, value, (size_t)len);
		(void)written;
	}
	close(fd);
}

static pid_t start_allocator_churn(size_t mib)
{
	int ready[2];
	pid_t pid;

	if (!mib)
		return -1;
	if (pipe(ready))
		die("pipe allocator churn");
	pid = fork();
	if (pid < 0)
		die("fork allocator churn");
	if (!pid) {
		long page = sysconf(_SC_PAGESIZE);
		const size_t chunk_bytes = 16 * 1024 * 1024;
		size_t chunks = (mib + 15) / 16;
		void **memory = calloc(chunks, sizeof(*memory));

		close(ready[0]);
		(void)setpriority(PRIO_PROCESS, 0, 19);
		set_oom_score_adj(-500);
		/* Let unlink start before pressure ramps, so an OOM victim does not
		 * prevent the test driver from entering the target path. */
		if (write(ready[1], "R", 1) != 1)
			_exit(2);
		close(ready[1]);
		if (!memory)
			_exit(3);
		for (;;) {
			size_t used = 0;

			while (used < chunks) {
				size_t bytes = chunk_bytes;
				volatile unsigned char *chunk;

				if ((used + 1) * 16 > mib)
					bytes = (mib - used * 16) * 1024 * 1024;
				chunk = malloc(bytes);
				if (!chunk)
					break;
				memory[used++] = (void *)chunk;
				for (size_t off = 0; off < bytes; off += (size_t)page)
					chunk[off] = (unsigned char)(off >> 12);
				/* Ramp pressure while unlink is already in progress. */
				usleep(25000);
			}
			usleep(10000);
			while (used)
				free(memory[--used]);
			usleep(5000);
		}
	}
	close(ready[1]);
	{
		char byte;

		if (read(ready[0], &byte, 1) != 1) {
			close(ready[0]);
			waitpid(pid, NULL, 0);
			errno = ENOMEM;
			die("start allocator churn");
		}
	}
	close(ready[0]);
	return pid;
}

static void drop_caches(void)
{
	const char value[] = "2\n";
	int fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);

	if (fd < 0)
		die("open drop_caches");
	if (write(fd, value, sizeof(value) - 1) != sizeof(value) - 1)
		die("write drop_caches");
	close(fd);
}

static void set_min_free(long kib)
{
	FILE *vm;

	if (kib <= 0)
		return;
	vm = fopen("/proc/sys/vm/min_free_kbytes", "we");
	if (!vm || fprintf(vm, "%ld\n", kib) < 0 || fclose(vm))
		die("set min_free_kbytes");
	printf("min_free_kbytes=%ld\n", kib);
	fflush(stdout);
}

static void seed_file(const char *dir, unsigned int iteration)
{
	char path[256], name[64];
	unsigned char value[VALUE_LEN];
	int fd;

	snprintf(path, sizeof(path), "%s/ea-%u", dir, iteration);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("open EA container");
	for (unsigned int i = 0; i < EA_COUNT; i++) {
		snprintf(name, sizeof(name), "user.a%03u", i);
		for (size_t j = 0; j < sizeof(value); j++)
			value[j] = (unsigned char)(j * 31 + iteration * 7 + i * 3);
		if (fsetxattr(fd, name, value, sizeof(value), 0)) {
			fprintf(stderr, "fsetxattr file=%u index=%u errno=%d\n",
				iteration, i, errno);
			die("fsetxattr EA seed");
		}
	}
	if (fsync(fd))
		die("fsync EA seed");
	if (close(fd))
		die("close EA container");
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-ea-churn/workload";
	unsigned int files = argc > 2 ? strtoul(argv[2], NULL, 10) : 64;
	size_t pressure_mib = argc > 3 ? strtoul(argv[3], NULL, 10) : 384;
	long min_free_kib = argc > 4 ? strtol(argv[4], NULL, 10) : 131072;
	char path[256];
	pid_t pressure;
	int dirfd;
	unsigned int removed = 0, remove_errors = 0, pressure_restarts = 0;

	if (!files || files > 4096)
		return 2;
	set_oom_score_adj(-1000);
	if (mkdir(dir, 0700) && errno != EEXIST)
		die("mkdir workload directory");
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		die("open workload directory");
	for (unsigned int i = 0; i < files; i++) {
		seed_file(dir, i);
		if (!(i % 16)) {
			printf("seeded_files=%u ea_per_file=%u\n", i + 1,
			       EA_COUNT);
			fflush(stdout);
		}
	}
	if (syncfs(dirfd))
		die("syncfs seeded containers");
	drop_caches();
	set_min_free(min_free_kib);
	pressure = start_allocator_churn(pressure_mib);
	printf("ea_ref_churn_start files=%u ea_per_file=%u churn_mib=%zu\n",
	       files, EA_COUNT, pressure_mib);
	fflush(stdout);
	if (pressure > 0)
		usleep(250000);
	for (unsigned int i = 0; i < files; i++) {
		snprintf(path, sizeof(path), "%s/ea-%u", dir, i);
		if (unlink(path)) {
			remove_errors++;
			fprintf(stderr, "unlink file=%u errno=%d\n", i, errno);
		} else {
			removed++;
		}
		if (pressure > 0 && !(i % 8)) {
			int status;
			pid_t reaped = waitpid(pressure, &status, WNOHANG);

			if (reaped == pressure) {
				pressure = start_allocator_churn(pressure_mib);
				pressure_restarts++;
				printf("pressure_restarts=%u\n", pressure_restarts);
				fflush(stdout);
			}
		}
		if (!(i % 16)) {
			printf("unlinked_files=%u\n", i + 1);
			fflush(stdout);
		}
	}
	if (pressure > 0) {
		kill(pressure, SIGTERM);
		waitpid(pressure, NULL, 0);
	}
	if (rmdir(dir))
		die("rmdir workload directory");
	if (syncfs(dirfd))
		die("syncfs released containers");
	close(dirfd);
	printf("ea_ref_churn_complete files=%u removed=%u remove_errors=%u pressure_restarts=%u\n",
	       files, removed, remove_errors, pressure_restarts);
	return 0;
}
